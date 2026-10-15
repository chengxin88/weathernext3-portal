# 数据与外部依赖

## 区域预报缓存

`WN3_ROOT` 指向已有的 `region-v4/weathernext3` 根目录。网页识别已验证的起报清单及原生 NPZ 场，主要包括：

- `run_YYYYMMDDHH.json`：起报身份、完成状态及已提交时次。
- `native-southern-china-v1/`：南部中国区域场。
- `native-china-v1/`：中国范围的已有原生场。
- `native-extras-v1/`：100 米风及站点头温度等附加变量的清单。
- `native-optional-v1/`：附加降水、云量与辐射变量的清单。

不同起报可能仅有部分资料。网页根据变量和连续累计时段的实际可用性展示产品。

`portal/weathernext3_native_cache.py` 和 `portal/weathernext3.py` 从当前公共模块整理而来。页面按本仓库模块读取既有区域场；其中保留的全局场兼容或采集辅助函数可能还依赖原后端框架，未作为独立上游下载管线打包。

## 资料采集

现有主机通过三维地球后端的 `prepare_weathernext3.py`、`prepare_weathernext3_china.py` 及对应 systemd 定时器更新资料。这套采集管线、云端登录和天气归档仍在原项目维护。

网页请求不会启动这些大范围采集任务。若迁往新机器，需迁移现有数据管线，或提供兼容的、已经验证和提交的缓存。

## 风电项目

- `WN3_WIND_PROJECT_ROOT`：WN3 风电项目配置、状态、逐日原生资料与已发布下载包。
- `EC_WIND_PROJECT_ROOT`：EC 风电项目的对应资料。
- `wind_project_worker.py`：WN3 100 米风原生时次采集和项目处理。
- `wind_project_15min.py`：项目 U/V 风分量的时间插值处理。

读取已发布项目资料不需要运行采集脚本。运行 `wind_project_worker.py` 的上游采集部分，需要 `WN3_BACKEND_ROOT` 指向提供 `prepare_weathernext3_china.RegionalClient` 的后端目录及其依赖，并由本机完成相应云端授权。具体站点配置、观测与下载包均作为运行资料放在仓库外。

## 运行状态与绘图

- `FORECAST_CACHE_ROOT/WN3_PORTAL/`：绘图缓存。
- `WN3_CATALOG_INDEX`：最近一次有效目录索引及刷新锁。
- 中文字体：优先使用仓库外数据目录中的 Noto 字体，或系统安装的 Noto CJK。
- `cities.json`、`geography.json`：随项目保存的页面地理资料。

历史部署脚本 `deploy_verify.py`、`verify_isolated.py` 引用固定的旧备份和旧图片命名，未作为新项目部署入口导入。
