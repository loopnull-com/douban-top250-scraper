#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""顺序抓取豆瓣电影 Top250，并支持断点续跑和封面下载。"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urljoin, urlparse


# 在命令行解析后再加载第三方依赖，使 --help 可在尚未安装依赖时使用。
requests: Any = None
BeautifulSoup: Any = None
Tag: Any = None
Image: Any = None
ImageOps: Any = None


BASE_URL = "https://movie.douban.com/top250"
PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "output"

EXPECTED_COUNT = 250
PAGE_SIZE = 25
REQUEST_DELAY_MIN = 1.0
REQUEST_DELAY_MAX = 2.0

CSV_COLUMNS = [
    "电影id",
    "电影封面",
    "电影名称",
    "豆瓣评分",
    "导演",
    "编剧",
    "主演",
    "类型",
    "制片国家/地区",
    "语言",
    "上映日期",
    "剧情简介",
]

FAILED_COLUMNS = ["电影id", "电影名称", "详情页", "失败阶段", "失败原因"]
CORE_FIELDS = ["电影id", "电影名称"]
OPTIONAL_FIELDS = [field for field in CSV_COLUMNS if field not in CORE_FIELDS]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)


def load_dependencies() -> None:
    """加载运行抓取所需的第三方库，并提供明确的缺失提示。"""
    global requests, BeautifulSoup, Tag, Image, ImageOps

    try:
        import requests as requests_module
        from bs4 import BeautifulSoup as beautiful_soup_class
        from bs4 import Tag as tag_class
        from PIL import Image as image_module
        from PIL import ImageOps as image_ops_module
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            f"缺少依赖 {exc.name}，请执行："
            "python -m pip install requests beautifulsoup4 pillow"
        ) from exc

    requests = requests_module
    BeautifulSoup = beautiful_soup_class
    Tag = tag_class
    Image = image_module
    ImageOps = image_ops_module


class CoverDownloadError(RuntimeError):
    """封面响应获取失败。"""


class CoverProcessError(RuntimeError):
    """封面内容无法转换为 JPEG。"""


class RequestClient:
    """维护会话，并确保相邻网络请求至少间隔 1～2 秒。"""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": BASE_URL,
            }
        )

        # Cookie 只从环境变量读取，避免把账号信息写入代码或日志。
        cookie = os.getenv("DOUBAN_COOKIE", "").strip()
        if cookie:
            self.session.headers["Cookie"] = cookie

        self._last_request_time: float | None = None

    def close(self) -> None:
        self.session.close()

    def _wait_before_request(self) -> None:
        if self._last_request_time is None:
            return

        target_interval = random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX)
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < target_interval:
            time.sleep(target_interval - elapsed)

    def get(
        self,
        url: str,
        *,
        purpose: str,
        referer: str | None = None,
        max_attempts: int = 3,
    ) -> requests.Response:
        """执行带超时、请求间隔和有限重试的 GET 请求。"""
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            self._wait_before_request()
            headers = {"Referer": referer} if referer else None

            try:
                response = self.session.get(
                    url,
                    headers=headers,
                    timeout=(10, 30),
                    allow_redirects=True,
                )
                self._last_request_time = time.monotonic()

                if response.status_code == 200:
                    return response

                if response.status_code in {403, 418, 429}:
                    raise RuntimeError(
                        f"{purpose}被网站限制访问，HTTP {response.status_code}。"
                        "匿名访问可能受到限制，可自行通过 DOUBAN_COOKIE "
                        "配置本人浏览器 Cookie"
                    )

                response.raise_for_status()
            except (requests.RequestException, RuntimeError) as exc:
                last_error = exc
                if attempt < max_attempts:
                    wait_seconds = 2 * attempt
                    print(
                        f"{purpose}失败（第 {attempt}/{max_attempts} 次）："
                        f"{exc}；{wait_seconds} 秒后重试"
                    )
                    time.sleep(wait_seconds)

        raise RuntimeError(f"{purpose}抓取失败：{last_error}")


def limit_value(value: str) -> int:
    """解析并校验 --limit。"""
    try:
        limit = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--limit 必须是 1～250 之间的整数") from exc
    if not 1 <= limit <= EXPECTED_COUNT:
        raise argparse.ArgumentTypeError("--limit 必须在 1～250 之间")
    return limit


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="顺序抓取豆瓣电影 Top250，支持断点续跑。"
    )
    parser.add_argument(
        "--limit",
        type=limit_value,
        default=EXPECTED_COUNT,
        metavar="N",
        help="最多处理前 N 部电影（1～250，默认：250）",
    )
    parser.add_argument(
        "--no-images",
        action="store_true",
        help="只保存文本数据，不下载或校验封面图片",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        metavar="PATH",
        help=f"输出目录（默认：{DEFAULT_OUTPUT_DIR}）",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="忽略已有结果并清理本程序管理的文件后重新抓取",
    )
    return parser.parse_args(argv)


def clean_text(value: object) -> str:
    """删除首尾空白，并将连续空白压缩为一个空格。"""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()


def join_values(values: Iterable[str], limit: int | None = None) -> str:
    """清理并去重多个值，使用“ / ”统一分隔。"""
    result: list[str] = []
    for value in values:
        item = clean_text(value)
        if item and item not in result:
            result.append(item)
        if limit is not None and len(result) >= limit:
            break
    return " / ".join(result)


def normalize_slash_values(value: str) -> str:
    """将网页中的斜杠分隔字段统一为“ / ”格式。"""
    return join_values(re.split(r"\s*/\s*", clean_text(value)))


def extract_movie_id(url: str) -> str:
    match = re.search(r"/subject/(\d+)/", url)
    if not match:
        raise ValueError(f"无法从链接提取电影 ID：{url}")
    return match.group(1)


def text_after_label(info: Tag | None, label: str) -> str:
    """读取指定标签之后、当前 br 之前的文本。"""
    if info is None:
        return ""

    for label_node in info.select("span.pl"):
        current_label = clean_text(label_node.get_text()).rstrip(":：")
        if current_label != label:
            continue

        parts: list[str] = []
        for sibling in label_node.next_siblings:
            if isinstance(sibling, Tag) and sibling.name == "br":
                break
            if isinstance(sibling, Tag):
                parts.append(sibling.get_text(" ", strip=True))
            else:
                parts.append(str(sibling))

        return clean_text(" ".join(parts).lstrip(" :："))

    return ""


def links_after_label(info: Tag | None, label: str) -> list[str]:
    """读取指定标签之后、当前 br 之前的所有人物链接文本。"""
    if info is None:
        return []

    for label_node in info.select("span.pl"):
        current_label = clean_text(label_node.get_text()).rstrip(":：")
        if current_label != label:
            continue

        names: list[str] = []
        for sibling in label_node.next_siblings:
            if isinstance(sibling, Tag) and sibling.name == "br":
                break
            if isinstance(sibling, Tag):
                names.extend(
                    clean_text(anchor.get_text(" ", strip=True))
                    for anchor in sibling.find_all("a")
                )
        return [name for name in names if name]

    return []


def people_by_rel(info: Tag | None, rel_value: str, limit: int | None = None) -> str:
    """按详情页 rel 属性提取导演或主演。"""
    if info is None:
        return ""

    names: list[str] = []
    for anchor in info.find_all("a"):
        rel = anchor.get("rel", [])
        rel_values = [rel] if isinstance(rel, str) else list(rel)
        if rel_value in rel_values:
            names.append(anchor.get_text(" ", strip=True))

    return join_values(names, limit=limit)


def parse_list_page(html: str) -> list[str]:
    """解析一个 Top250 列表页，返回电影详情页链接。"""
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []

    for anchor in soup.select("ol.grid_view li .hd a[href]"):
        href = clean_text(anchor.get("href"))
        match = re.search(r"/subject/(\d+)/", href)
        if not match:
            continue

        url = f"https://movie.douban.com/subject/{match.group(1)}/"
        if url not in urls:
            urls.append(url)

    return urls


def fetch_detail_urls(client: RequestClient, limit: int) -> list[str]:
    """仅抓取覆盖目标数量所需的榜单页，并返回榜单顺序的详情链接。"""
    urls: list[str] = []
    page_count = (limit + PAGE_SIZE - 1) // PAGE_SIZE

    for page_number in range(1, page_count + 1):
        start = (page_number - 1) * PAGE_SIZE
        page_url = f"{BASE_URL}?start={start}&filter="
        response = client.get(
            page_url, purpose=f"列表页 {page_number}/{page_count}"
        )
        page_urls = parse_list_page(response.text)

        if len(page_urls) != PAGE_SIZE:
            raise RuntimeError(
                f"列表页 {page_number} 应有 {PAGE_SIZE} 条，"
                f"实际解析到 {len(page_urls)} 条"
            )

        for url in page_urls:
            if url not in urls:
                urls.append(url)

        print(
            f"列表页 {page_number}/{page_count}："
            f"累计获得 {min(len(urls), limit)} 个目标详情链接"
        )

    urls = urls[:limit]
    if len(urls) != limit:
        raise RuntimeError(f"应获得 {limit} 个唯一详情链接，实际为 {len(urls)} 个")
    return urls


def validate_detail_response(response: requests.Response, source_url: str) -> None:
    """确认 HTTP 200 响应仍是所请求的正常电影详情页。"""
    expected_id = extract_movie_id(source_url)
    final_url = response.url
    parsed_url = urlparse(final_url)
    expected_path = f"/subject/{expected_id}/"
    soup = BeautifulSoup(response.text, "html.parser")

    title = clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    page_text = clean_text(soup.get_text(" ", strip=True))
    restriction_markers = (
        "访问过于频繁",
        "检测到有异常请求",
        "异常请求",
        "安全验证",
        "验证码",
        "登录豆瓣",
        "访问受限",
        "访问受到限制",
    )
    has_detail_structure = soup.select_one("h1") is not None and soup.select_one(
        "#info"
    ) is not None
    is_restriction_page = (
        parsed_url.hostname == "sec.douban.com"
        or (
            not has_detail_structure
            and any(
                marker in title or marker in page_text
                for marker in restriction_markers
            )
        )
    )
    if is_restriction_page:
        raise RuntimeError(
            "详情页访问受到限制，未获得正常电影页面。"
            f"最终 URL：{final_url}。匿名访问可能受到限制，可自行通过 "
            "DOUBAN_COOKIE 配置本人浏览器 Cookie"
        )

    is_expected_url = (
        parsed_url.hostname == "movie.douban.com"
        and parsed_url.path.rstrip("/") == expected_path.rstrip("/")
    )
    if not is_expected_url or not has_detail_structure:
        raise RuntimeError(f"详情页返回非预期页面。最终 URL：{final_url}")


def parse_movie_detail(html: str, source_url: str) -> dict[str, str]:
    """解析电影详情页，生成固定的 12 个导出字段。"""
    soup = BeautifulSoup(html, "html.parser")
    info = soup.select_one("#info")

    title_node = soup.select_one('h1 span[property="v:itemreviewed"]')
    if title_node is None:
        title_node = soup.select_one("h1 span")

    rating_node = soup.select_one('[property="v:average"]')
    cover_node = soup.select_one("#mainpic img[src]")

    summary_node = soup.select_one("#link-report-intra span.all.hidden")
    if summary_node is None:
        summary_node = soup.select_one(
            '#link-report-intra span[property="v:summary"]'
        )

    genres = [
        node.get_text(" ", strip=True)
        for node in soup.select('#info span[property="v:genre"]')
    ]
    release_dates = [
        node.get_text(" ", strip=True)
        for node in soup.select('#info span[property="v:initialReleaseDate"]')
    ]

    record = {
        "电影id": extract_movie_id(source_url),
        "电影封面": clean_text(cover_node.get("src")) if cover_node else "",
        # 详情页标题通常同时包含中文名和英文名。
        "电影名称": clean_text(title_node.get_text(" ", strip=True))
        if title_node
        else "",
        "豆瓣评分": clean_text(rating_node.get_text(" ", strip=True))
        if rating_node
        else "",
        "导演": people_by_rel(info, "v:directedBy"),
        "编剧": join_values(links_after_label(info, "编剧")),
        # 主演最多保留前三位，避免导出字段过长。
        "主演": people_by_rel(info, "v:starring", limit=3),
        "类型": join_values(genres),
        "制片国家/地区": normalize_slash_values(
            text_after_label(info, "制片国家/地区")
        ),
        "语言": normalize_slash_values(text_after_label(info, "语言")),
        "上映日期": join_values(release_dates),
        "剧情简介": clean_text(summary_node.get_text(" ", strip=True))
        if summary_node
        else "",
    }

    missing_core = [field for field in CORE_FIELDS if not record[field]]
    if missing_core:
        raise RuntimeError(f"缺少核心字段：{', '.join(missing_core)}")

    missing_optional = [field for field in OPTIONAL_FIELDS if not record[field]]
    if missing_optional:
        print(
            f"warning：电影 {record['电影id']} 缺少可选字段："
            f"{', '.join(missing_optional)}"
        )

    return record


def save_cover(
    client: RequestClient,
    image_dir: Path,
    movie_id: str,
    cover_url: str,
    source_url: str,
) -> None:
    """下载封面并真正转换为 JPEG，而不是只修改扩展名。"""
    if not cover_url:
        raise CoverDownloadError("详情数据中没有封面 URL")

    image_dir.mkdir(parents=True, exist_ok=True)
    output_path = image_dir / f"{movie_id}.jpg"
    temporary_path = image_dir / f".{movie_id}.jpg.tmp"

    try:
        response = client.get(
            urljoin(source_url, cover_url),
            purpose=f"电影 {movie_id} 封面",
            referer=source_url,
        )
    except RuntimeError as exc:
        raise CoverDownloadError(str(exc)) from exc

    try:
        with Image.open(io.BytesIO(response.content)) as source_image:
            source_image.load()
            image = ImageOps.exif_transpose(source_image)

            if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                rgba = image.convert("RGBA")
                rgb = Image.new("RGB", rgba.size, "white")
                rgb.paste(rgba, mask=rgba.getchannel("A"))
            else:
                rgb = image.convert("RGB")

            rgb.save(
                temporary_path,
                format="JPEG",
                quality=92,
                optimize=True,
            )
        temporary_path.replace(output_path)
    except Exception as exc:
        # 单张图片的解码或转换错误只影响当前电影，并由上层记录阶段。
        temporary_path.unlink(missing_ok=True)
        raise CoverProcessError(
            f"电影 {movie_id} 封面不是可转换的有效图片：{exc}"
        ) from exc


def normalize_record(row: dict[str, str | None]) -> dict[str, str]:
    """将 CSV 行规范为当前字段集合，兼容新增的空字段。"""
    return {field: clean_text(row.get(field)) for field in CSV_COLUMNS}


def read_records(csv_path: Path) -> tuple[dict[str, dict[str, str]], list[str]]:
    """读取已有成功数据，返回 ID 映射和原有顺序。"""
    if not csv_path.exists():
        return {}, []

    records: dict[str, dict[str, str]] = {}
    order: list[str] = []
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            if reader.fieldnames is None or not set(CORE_FIELDS).issubset(
                reader.fieldnames
            ):
                raise RuntimeError("缺少电影id或电影名称列")
            for row_number, row in enumerate(reader, 2):
                record = normalize_record(row)
                movie_id = record["电影id"]
                if not movie_id or not record["电影名称"]:
                    print(f"warning：忽略 top250.csv 第 {row_number} 行的无效记录")
                    continue
                if movie_id not in records:
                    order.append(movie_id)
                else:
                    print(f"warning：top250.csv 中电影 ID {movie_id} 重复，保留后者")
                records[movie_id] = record
    except (OSError, csv.Error, UnicodeError) as exc:
        raise RuntimeError(f"无法读取已有 top250.csv：{exc}") from exc

    print(f"已读取 {len(records)} 条已有数据")
    return records, order


def read_failures(failed_path: Path) -> dict[str, dict[str, str]]:
    """读取尚未解决的失败项。"""
    if not failed_path.exists():
        return {}

    failures: dict[str, dict[str, str]] = {}
    try:
        with failed_path.open("r", encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            if reader.fieldnames is None or "电影id" not in reader.fieldnames:
                print("warning：已有 failed.csv 格式不兼容，将重新生成")
                return {}
            for row in reader:
                movie_id = clean_text(row.get("电影id"))
                if movie_id:
                    failures[movie_id] = {
                        field: clean_text(row.get(field)) for field in FAILED_COLUMNS
                    }
    except (OSError, csv.Error, UnicodeError) as exc:
        raise RuntimeError(f"无法读取已有 failed.csv：{exc}") from exc
    return failures


def ordered_records(
    records: dict[str, dict[str, str]],
    target_ids: Sequence[str],
    previous_order: Sequence[str],
) -> list[dict[str, str]]:
    """按当前榜单目标顺序输出，并保留本次范围外的已有记录。"""
    output_ids: list[str] = []
    preserved_ids = previous_order if len(target_ids) < EXPECTED_COUNT else []
    for movie_id in [*target_ids, *preserved_ids]:
        if movie_id in records and movie_id not in output_ids:
            output_ids.append(movie_id)
    return [records[movie_id] for movie_id in output_ids]


def atomic_write_csv(
    path: Path,
    rows: Iterable[dict[str, str]],
    fieldnames: Sequence[str],
) -> None:
    temporary_path = path.with_name(f".{path.name}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        temporary_path.replace(path)
    except (OSError, csv.Error) as exc:
        temporary_path.unlink(missing_ok=True)
        raise RuntimeError(f"无法写入 {path}：{exc}") from exc


def save_records(
    records: dict[str, dict[str, str]],
    target_ids: Sequence[str],
    previous_order: Sequence[str],
    csv_path: Path,
    json_path: Path,
) -> None:
    """同步、原子地更新 CSV 与 JSON。"""
    rows = ordered_records(records, target_ids, previous_order)
    atomic_write_csv(csv_path, rows, CSV_COLUMNS)

    temporary_path = json_path.with_name(f".{json_path.name}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(rows, file, ensure_ascii=False, indent=2)
            file.write("\n")
        temporary_path.replace(json_path)
    except (OSError, TypeError) as exc:
        temporary_path.unlink(missing_ok=True)
        raise RuntimeError(f"无法写入 {json_path}：{exc}") from exc


def save_failures(
    failures: dict[str, dict[str, str]], failed_path: Path
) -> None:
    """始终生成 failed.csv；无失败时文件仅含表头。"""
    atomic_write_csv(failed_path, failures.values(), FAILED_COLUMNS)


def record_failure(
    failures: dict[str, dict[str, str]],
    movie_id: str,
    detail_url: str,
    stage: str,
    reason: str,
    name: str = "",
) -> None:
    failures[movie_id] = {
        "电影id": movie_id,
        "电影名称": name,
        "详情页": detail_url,
        "失败阶段": stage,
        "失败原因": clean_text(reason),
    }


def clean_managed_outputs(output_dir: Path) -> None:
    """只清理本程序管理的数据文件和数字 ID 命名的封面。"""
    for filename in (
        "top250.csv",
        "top250.json",
        "failed.csv",
        ".top250.csv.tmp",
        ".top250.json.tmp",
        ".failed.csv.tmp",
    ):
        (output_dir / filename).unlink(missing_ok=True)

    image_dir = output_dir / "images"
    if image_dir.is_dir():
        for image_path in image_dir.glob("*.jpg"):
            if image_path.stem.isdigit():
                image_path.unlink()
        for temporary_path in image_dir.glob(".*.jpg.tmp"):
            if temporary_path.name[1:-8].isdigit():
                temporary_path.unlink()


def validate_result(
    records: dict[str, dict[str, str]],
    target_ids: Sequence[str],
    image_dir: Path,
    no_images: bool,
) -> list[str]:
    """检查目标数据数量、ID 唯一性以及所需图片的对应关系。"""
    completed_records = [
        records[movie_id] for movie_id in target_ids if movie_id in records
    ]
    completed_ids = [record["电影id"] for record in completed_records]
    problems: list[str] = []

    if len(completed_ids) != len(set(completed_ids)):
        problems.append("目标数据中存在重复电影 ID")
    if len(completed_records) != len(target_ids):
        problems.append(
            f"目标 {len(target_ids)} 条，当前完整数据 {len(completed_records)} 条"
        )
    elif completed_ids != list(target_ids):
        problems.append("数据记录中的电影 ID 与榜单目标不一致")

    if not no_images:
        missing_images = [
            movie_id
            for movie_id in completed_ids
            if not (image_dir / f"{movie_id}.jpg").is_file()
        ]
        if missing_images:
            problems.append(f"有 {len(missing_images)} 条数据缺少对应封面")
        if len(target_ids) == EXPECTED_COUNT and image_dir.is_dir():
            managed_image_ids = {
                path.stem for path in image_dir.glob("*.jpg") if path.stem.isdigit()
            }
            if managed_image_ids != set(completed_ids):
                problems.append("数据记录与本项目生成的封面文件未一一对应")

    return problems


def run(args: argparse.Namespace) -> int:
    output_dir = args.output.expanduser().resolve()
    csv_path = output_dir / "top250.csv"
    json_path = output_dir / "top250.json"
    failed_path = output_dir / "failed.csv"
    image_dir = output_dir / "images"

    output_dir.mkdir(parents=True, exist_ok=True)
    if args.fresh:
        clean_managed_outputs(output_dir)
        print(f"已清理本程序管理的旧结果：{output_dir}")

    records, previous_order = read_records(csv_path)
    failures = read_failures(failed_path)
    # 启动时即以 CSV 为准同步 JSON，便于旧结果升级后直接续跑。
    save_records(records, [], previous_order, csv_path, json_path)
    save_failures(failures, failed_path)

    client = RequestClient()
    added = 0
    repaired_covers = 0
    skipped = 0
    successful_this_run = 0
    failed_this_run = 0

    try:
        detail_urls = fetch_detail_urls(client, args.limit)
        target_ids = [extract_movie_id(url) for url in detail_urls]

        for index, detail_url in enumerate(detail_urls, 1):
            movie_id = target_ids[index - 1]
            record = records.get(movie_id)
            image_path = image_dir / f"{movie_id}.jpg"

            if record is not None and (args.no_images or image_path.is_file()):
                skipped += 1
                failures.pop(movie_id, None)
                save_failures(failures, failed_path)
                print(
                    f"[{index:03d}/{args.limit}] 电影 {movie_id} 已完整完成，跳过"
                )
                continue

            if record is None:
                try:
                    response = client.get(
                        detail_url,
                        purpose=f"详情页 {index}/{args.limit}",
                        referer=BASE_URL,
                    )
                except RuntimeError as exc:
                    failed_this_run += 1
                    record_failure(
                        failures,
                        movie_id,
                        detail_url,
                        "detail_request",
                        str(exc),
                    )
                    save_failures(failures, failed_path)
                    print(
                        f"[{index:03d}/{args.limit}] 电影 {movie_id} "
                        f"处理失败（detail_request）：{exc}"
                    )
                    continue

                try:
                    validate_detail_response(response, detail_url)
                    record = parse_movie_detail(response.text, detail_url)
                except Exception as exc:
                    # 详情页结构异常仅记为当前电影失败，不终止整个榜单任务。
                    failed_this_run += 1
                    record_failure(
                        failures,
                        movie_id,
                        detail_url,
                        "detail_parse",
                        str(exc),
                    )
                    save_failures(failures, failed_path)
                    print(
                        f"[{index:03d}/{args.limit}] 电影 {movie_id} "
                        f"处理失败（detail_parse）：{exc}"
                    )
                    continue

                records[movie_id] = record
                added += 1
                # 详情成功后立即持久化；即使封面失败，下次也只需补图。
                save_records(
                    records,
                    target_ids,
                    previous_order,
                    csv_path,
                    json_path,
                )

            if args.no_images:
                successful_this_run += 1
                failures.pop(movie_id, None)
                save_failures(failures, failed_path)
                print(
                    f"[{index:03d}/{args.limit}] {record['电影名称']}：文本信息已保存"
                )
                continue

            was_existing_record = movie_id in previous_order
            try:
                save_cover(
                    client,
                    image_dir,
                    movie_id,
                    record["电影封面"],
                    detail_url,
                )
            except CoverDownloadError as exc:
                failed_this_run += 1
                record_failure(
                    failures,
                    movie_id,
                    detail_url,
                    "cover_download",
                    str(exc),
                    record["电影名称"],
                )
                save_failures(failures, failed_path)
                print(
                    f"[{index:03d}/{args.limit}] 电影 {movie_id} "
                    f"处理失败（cover_download）：{exc}"
                )
                continue
            except CoverProcessError as exc:
                failed_this_run += 1
                record_failure(
                    failures,
                    movie_id,
                    detail_url,
                    "cover_process",
                    str(exc),
                    record["电影名称"],
                )
                save_failures(failures, failed_path)
                print(
                    f"[{index:03d}/{args.limit}] 电影 {movie_id} "
                    f"处理失败（cover_process）：{exc}"
                )
                continue

            if was_existing_record:
                repaired_covers += 1
            successful_this_run += 1
            failures.pop(movie_id, None)
            save_failures(failures, failed_path)
            print(
                f"[{index:03d}/{args.limit}] {record['电影名称']}："
                "信息和封面处理完成"
            )

        # 即使全部记录都来自断点，也确保 JSON 与 CSV 同步存在。
        save_records(
            records,
            target_ids,
            previous_order,
            csv_path,
            json_path,
        )
        save_failures(failures, failed_path)

        problems = validate_result(records, target_ids, image_dir, args.no_images)
        print(
            f"完成：本次成功 {successful_this_run}，新增数据 {added}，"
            f"补全封面 {repaired_covers}，"
            f"跳过已有 {skipped}，失败 {failed_this_run}。"
        )
        if problems:
            print("结果检查：" + "；".join(problems))
        else:
            print(f"结果检查通过，输出目录：{output_dir}")
        return 0 if not problems else 1
    finally:
        client.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    load_dependencies()
    return run(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("程序已由用户中断。")
        raise SystemExit(130)
    except Exception as exc:
        print(f"程序运行失败：{exc}")
        raise SystemExit(1)
