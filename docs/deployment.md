# 部署说明

## 现有服务

- 用户服务：`weather-wn3-portal.service`。
- 当前工作目录：`/home/chengyang/ec-gfs-server/wn3_portal`。
- 当前 Python：`/home/chengyang/.venvs/ec-gfs-server/bin/python`。
- 当前端口：`127.0.0.1:8515`。
- 对外入口：`https://weather.szverify.xyz/wn3/`。

## 验证仓库版本

1. 克隆本私有仓库，通过本机 Git 授权访问。
2. 安装 `requirements.txt`，或在现有 WSL 使用原虚拟环境。
3. 按根目录 README 配置 `WN3_ROOT`，用独立 `FORECAST_CACHE_ROOT`、`WN3_CATALOG_INDEX` 和空闲端口预览。
4. 验证目录、地图、资料下载及项目资料；有些项目数据目录为空时，对应下载接口会返回不可用。
5. 独立主机还需配置中文字体、已有气象数据和上游更新管线。

## 切换现有 WSL 服务

验证后记录要上线的 Git 提交，并备份现有服务配置。将服务工作目录改为仓库的 `portal/`，设置所需环境变量和可写缓存、状态目录。用户服务模板见 [`../deploy/weather-wn3-portal.service.example`](../deploy/weather-wn3-portal.service.example)。

修改服务文件后执行：

```bash
systemctl --user daemon-reload
systemctl --user restart weather-wn3-portal.service
systemctl --user status weather-wn3-portal.service
```

通过原站点验证页面、目录、指定起报产品和下载。若需回退，将服务工作目录恢复到原目录或前一个已验证提交后重启。

## 反向代理

[`../deploy/nginx-location.conf.example`](../deploy/nginx-location.conf.example) 展示 `/wn3/` 到本机服务的路径映射。将规则接入现有登录网关与 HTTPS 站点配置；Python 服务保持仅监听本机。

## 更新代码与数据

GitHub 管理源码。后续获取代码时先在预览端口验证，再更新运行服务；本仓库没有连接生产服务的自动部署工作流。

原有 WN3 下载服务和定时器继续写入天气归档，网页读取其已发布时次。不要将原始气象资料、ADC 凭据或用户服务中的实际授权配置提交到 Git。
