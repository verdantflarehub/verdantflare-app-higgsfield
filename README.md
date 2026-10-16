# Higgsfield 渠道

内部渠道服务与 AppDashboard。管理监听 8001，执行监听 8000。账号 OAuth、账本与单写互斥沿用现有受控存储；不提供独立公网 MCP。

轻量检查：`python -m unittest discover -s tests`；Dashboard 打包：`python -m higgsfield_gateway.dashboard --source dashboard --output <空目录>`。

源码从 `verdantflarehub/verdantflare-app-video` 提交 `1a6c5422c77c29732fcbfd07a14c3aca907f7ead` 拆分，原始历史保留在来源仓库；fal 同时保留迁移时尚未提交的渠道账号实现。当前目录为唯一源码入口。产品与部署事实统一维护于 `verdantflare-design/docs/design/app/higgsfield/README.md` 和设计仓库部署目录。独立工作流保留原镜像命名，下一发行递增版本；迁移不重打已发布标签。
