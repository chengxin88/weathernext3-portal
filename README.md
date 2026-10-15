# WeatherNext 3 预报产品平台

WN3 区域预报地图、原生场下载与风电项目资料页面。源码整理自 2026-10-15 正在运行的 WSL 服务：`/home/chengyang/ec-gfs-server/wn3_portal`。

服务入口为 [`portal/server.py`](portal/server.py)。原站点入口为 `https://weather.szverify.xyz/wn3/`。

## 功能

- 常规、IMERG 及试验降水产品的 1、3、6、12、24 小时累计图。
- 2 米气温、露点、相对湿度、24 小时变温，10 米及 100 米风、海平面气压。
- 数据已准备时展示云量和短波辐射产品。
- 按起报时间、预报时效、区域选图，支持链接参数和降水数值显示。
- 原生场及已发布风电项目资料的单文件、ZIP 下载。
- 本地目录索引与后台刷新；HTTP 请求复用最近一次有效目录。

产品可用性以本地已提交的资料为准。页面读取已有 WN3 集合统计资料，原生分辨率按变量区分：格点统计场主要为 0.1°，部分站点头温度场为 0.05°。

## 项目结构

| 位置 | 用途 |
| --- | --- |
| [`portal/server.py`](portal/server.py) | 本机 HTTP 服务、页面与 API |
| [`portal/render.py`](portal/render.py) | 产品定义、区域、科学绘图和 PNG 缓存 |
| [`portal/catalog_index.py`](portal/catalog_index.py) | 持久化目录索引和后台扫描 |
| [`portal/static/`](portal/static/) | 主页面、资料下载及风电项目页面 |
| [`portal/downloads.py`](portal/downloads.py) | 原生场清单及下载 |
| [`portal/wind_projects.py`](portal/wind_projects.py) | WN3 风电项目资料读取与下载 |
| [`portal/ec_wind_projects.py`](portal/ec_wind_projects.py) | EC 风电项目资料读取与下载 |
| [`portal/weathernext3_native_cache.py`](portal/weathernext3_native_cache.py) | 随仓库整理的原生缓存读取模块 |
| [`portal/weathernext3.py`](portal/weathernext3.py) | 原生读取模块使用的公共时间及数据源工具 |
| [`deploy/`](deploy/) | 用户服务、反向代理示例 |
| [`docs/`](docs/) | 数据依赖、源码来源和整理说明 |

## 在现有 WSL 环境运行

需要 Linux/WSL、Python 3.12、中文字体，以及已经下载的 WN3 区域资料。源码包含网页读取所需的两个共享模块，保留现有业务格式；天气缓存和项目资料另行提供。

```bash
cd /path/to/weathernext3-portal/portal
export WN3_ROOT=/home/chengyang/gods-eye-weather/weather-data/region-v4/weathernext3
export FORECAST_CACHE_ROOT=/tmp/wn3-portal-preview-cache
export WN3_CATALOG_INDEX=/tmp/wn3-portal-preview-state/catalog-v1.json
/home/chengyang/.venvs/ec-gfs-server/bin/python server.py --port 8516
```

使用空闲端口后访问 `http://127.0.0.1:8516/`。首次建立目录索引可能需要等待；有已验证的索引后会优先响应已有目录。运行预览时使用独立缓存和状态目录。

新环境可先安装 Python 依赖：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Ubuntu 可通过系统包提供 Noto CJK 中文字体。绘图需要可用的 Matplotlib、Cartopy/PROJ 环境；随仓库的 `cities.json`、`geography.json` 是页面地理资料。

## 配置与部署

配置字段见 [`.env.example`](.env.example)，程序通过环境变量读取配置，未内置 dotenv 加载器。部署模板与现有服务切换步骤见 **[部署说明](docs/deployment.md)**。

网页服务只监听 `127.0.0.1`，由现有反向代理将 `/wn3/` 映射到本机服务，并提供原站点的访问控制。

资料采集与网页服务分别运行。上游 WN3 下载定时器、云端授权和气象归档仍由当前数据管线维护；本仓库包含风电项目处理脚本，但执行其下载部分还需要上游 `prepare_weathernext3_china.RegionalClient`。更多依赖见 [数据说明](docs/data-and-dependencies.md)。

## 校验

现有目录与 HTTP 测试只使用临时资料和本仓库的历史回归样本：

```bash
cd portal
NO_PROXY=127.0.0.1,localhost python -m unittest test_catalog_index
node --test test_frontend.js
```

测试覆盖已提交时次、目录优先级、失效索引回退、刷新锁、并发响应，以及前端准备中、超时、登录失效和重试处理。

## 版本管理

本仓库为私有源码仓库。运行缓存、真实气象场、云端凭据、日志和备份由 `.gitignore` 排除。源码来源和少量路径适配记录在 [源码清单](docs/source-manifest.json) 中；本次没有切换线上服务目录。

上游 WN3 数据及地理资料的使用与再分发遵循各自来源条款。本次整理未新增源码许可声明。
