# Douban Top250 Scraper

豆瓣电影 Top250 数据抓取工具。

一个基于 Python、Requests、BeautifulSoup 和 Pillow 的轻量级数据抓取项目，可按榜单顺序获取豆瓣电影 Top250 的电影信息，导出 CSV / JSON 数据，并可选下载电影封面。

## 功能特性

* 抓取豆瓣电影 Top250 榜单及电影详情
* 导出 CSV / JSON 两种数据格式
* 下载电影封面，并使用 Pillow 实际转换为 JPEG
* 支持断点续跑
* 单部电影失败时隔离错误，不中断整个任务
* 使用 `failed.csv` 记录失败项目及失败阶段
* 支持限制本次处理数量
* 支持关闭封面下载
* 支持自定义输出目录
* 支持清理程序管理的旧结果后重新抓取
* 支持通过 `DOUBAN_COOKIE` 环境变量传入 Cookie
* 采用低频顺序请求，不进行并发抓取
* 请求间随机等待 1～2 秒，单次请求最多尝试 3 次
* 可识别 HTTP 403 / 418 / 429，以及 HTTP 200 安全检查页等访问限制

## 抓取字段

每条电影记录包含以下字段：

* `电影id`
* `电影封面`
* `电影名称`
* `豆瓣评分`
* `导演`
* `编剧`
* `主演`（最多前三位）
* `类型`
* `制片国家/地区`
* `语言`
* `上映日期`
* `剧情简介`

## 环境要求

* Python 3.10+
* requests
* beautifulsoup4
* Pillow

Python 3.10+ 是由当前程序使用的类型注解语法决定的。

## 安装

```bash
git clone https://github.com/loopnull-com/douban-top250-scraper.git
cd douban-top250-scraper
python -m pip install -r requirements.txt
```

## 基本使用

在项目根目录运行：

```bash
python douban_top250.py
```

默认处理 Top250 全部 250 部电影，并将结果写入项目目录下的 `output/`。

查看完整命令行帮助：

```bash
python douban_top250.py --help
```

## 命令行参数

### `--limit N`

最多处理榜单前 N 部电影，范围为 `1～250`。

```bash
python douban_top250.py --limit 10
```

### `--no-images`

只保存电影数据，不下载或校验封面。

```bash
python douban_top250.py --no-images
```

### `--output PATH`

指定输出目录。

```bash
python douban_top250.py --output ./my-output
```

### `--fresh`

清理指定输出目录中由本程序管理的旧结果后重新抓取。

包括：

* `top250.csv`
* `top250.json`
* `failed.csv`
* `images/` 中以数字电影 ID 命名的封面文件

不会删除输出目录中的未知文件。

```bash
python douban_top250.py --fresh
```

参数可以组合使用：

```bash
python douban_top250.py --limit 10 --no-images
```

如需生成一套独立的小范围测试数据，可以结合单独输出目录：

```bash
python douban_top250.py --limit 10 --fresh --output ./output-test
```

## 输出结构

默认输出结构：

```text
output/
├─ top250.csv
├─ top250.json
├─ failed.csv
└─ images/
   ├─ <电影ID>.jpg
   └─ ...
```

### `top250.csv`

使用 UTF-8 BOM 编码，便于 Excel 等常见表格软件直接打开。

### `top250.json`

以 JSON 数组形式保存电影记录，字段与 CSV 保持一致。

### `failed.csv`

记录尚未成功处理的项目，包含：

* 电影 ID
* 电影名称
* 详情页
* 失败阶段
* 失败原因

没有失败项目时，文件仅保留表头。

使用 `--no-images` 时不会创建或校验 `images/` 中的封面。

## 断点续跑

程序会根据已有的 `top250.csv` 和封面文件判断任务完成状态：

* 数据和对应封面均存在：直接跳过
* 数据存在但封面缺失：仅补充封面
* 封面存在但没有数据记录：重新抓取详情数据
* 使用 `--no-images`：只检查电影数据是否存在
* 某个之前失败的电影成功处理后，会从 `failed.csv` 中移除对应失败记录

程序会在每部电影成功处理后及时持久化结果，因此任务被中断后可以继续运行，而不需要重新处理全部电影。

小范围运行不会删除 `top250.csv` 中本次范围之外的已有记录。如需一套全新的小范围结果，建议使用独立的 `--output` 目录并配合 `--fresh`。

## Cookie

程序支持通过 `DOUBAN_COOKIE` 环境变量传入用户自己的浏览器 Cookie。

Windows PowerShell 示例：

```powershell
$env:DOUBAN_COOKIE="your_cookie_here"
python douban_top250.py
```

Cookie 仅从环境变量读取。

请勿：

* 将真实 Cookie 写入源码
* 将 Cookie 提交到 GitHub
* 提交包含 Cookie 的 `.env` 或其他本地配置文件

提供 Cookie 并不代表一定能够正常访问页面，目标网站仍可能根据访问环境触发安全检查或其他限制。

## 访问限制

豆瓣可能对电影详情页启用访问控制。

已知情况下，详情页请求可能：

1. 请求 `movie.douban.com/subject/<id>/`
2. 返回 HTTP `302`
3. 跳转至 `sec.douban.com`
4. 最终返回 HTTP `200` 的安全检查页面，而非实际电影详情页

因此，HTTP `200` 并不一定意味着成功获取电影页面。

程序会尝试识别：

* HTTP 403
* HTTP 418
* HTTP 429
* 重定向至豆瓣安全检查页面
* HTTP 200 但页面内容并非正常电影详情页

遇到此类情况时，程序会将其报告为访问受限，而不是误判为电影字段解析失败。

本项目不会尝试绕过验证码、安全检查或其他访问控制。

## 注意事项

* 本项目主要用于 Python 网页解析和数据处理学习
* 使用者应遵守目标网站的服务条款、相关规则及适用法律
* 不建议提高请求频率或改造成高并发抓取程序
* 网站页面结构变化可能导致部分字段解析失效
* 不同网络环境下的访问限制情况可能不同
* `DOUBAN_COOKIE` 只能作为用户自行提供的访问凭据，不保证能够解除访问限制
* 项目不会自动提取浏览器 Cookie、使用代理池或绕过验证码

## License

本项目基于 [GNU General Public License v3.0](LICENSE) 发布。

你可以使用、修改和再发布本项目，但基于本项目发布的衍生版本同样需要遵守 GPL v3.0 的开源要求。

## 作者

**loopnull**
