# 棠原试验室碳因子结算册

合成用电/燃料/出行活动→因子核算与差异说明→站内周期结算；范围声明与缺因子拒绝

## 业务场景与工作流
模拟客户：棠原研究协作组，12位业务与分析人员。每月研究明细先从`carbon-factors-202611.csv`录入底稿，经口径冻结、Python核算、负责人复核后向业务阅读者交付站内CSV。原有流程依靠人工表格抄算，痛点为口径版本难追溯、重复计算易错、复核与交付脱节。

模块：业务总览、业务底稿、新建口径版本、异步核算与复核、定时交付、站内收件箱、审计。底稿可新增/修改/删除；口径仅新增版本；报告提交时冻结来源与版本并保存SHA256。定时交付引用已复核报告快照，worker到期写入数据库，业务阅读者下载。

## 算法口径
单位转换、因子版本冻结、用量×因子与部门透视。kWh/MWh单位换算手算；缺单位因子拒绝；因子更新旧报告不变。

活动量 × 因子 · 单位与范围随报告保存。输入：业务底稿与口径JSON；输出：summary、透视rows、method、warnings。算法实现在`backend/src/services/calculation.py`，输入校验在`validation.py`。固定seed=42，240条合成数据；未对接真实业务系统。修改源数据和新建因子/口径不会改变已提交报告。

## 一天的验收剧本
1. 09:00 admin查看240条seed底稿与具名业务期间。
2. 09:10 analyst添加`carbon-factors-202611.csv`的一条具名业务记录。
3. 09:20 admin在口径版本页登记新版本；旧版本保留。
4. 09:30 analyst命名“十一月核算”并选择期间/版本排队。
5. 09:31 Redis worker真实执行Python计算，展示汇总/透视与口径边界。
6. 09:40 admin核对数值后填写至少4字复核说明。
7. 09:45 admin建立60秒周期计划，worker到期生成站内持久化交付。
8. 09:46 viewer在站内收件箱下载交付CSV；审计页查看操作者与时间。
9. 10:00 admin 发现电力因子登记有误，登记 `因子-v2`；在“因子勘误”页选旧/新版本与“电力”发起批次，核对逐行差额后整组复核并原子切换交付计划；原报告与已发交付保留。

## 快速启动
```sh
cp .env.example .env
docker compose up -d --build
docker compose exec -T backend python -m scripts.seed_demo
```
前端 http://localhost:18254 ，后端 http://localhost:19254/docs ，数据库宿主端口17754。中文父目录可用。恰好5服务frontend/backend/worker/db/redis；均有真实healthcheck。停止并清空演示数据：`docker compose down -v --remove-orphans`。

|角色|邮箱|密码|权限|
|---|---|---|---|
|admin|admin@demo.local|Demo2026!|全部底稿、口径版本、核算、复核、调度|
|analyst|analyst@demo.local|Demo2026!|底稿增改删、核算、导出；无复核/调度/口径写权限|
|viewer|viewer@demo.local|Demo2026!|只读；仅可下载已复核报告与站内交付|

## 工程结构与开发
Python3.11/FastAPI/SQLAlchemy/Redis，PostgreSQL16；前端原生HTML/CSS/JS ES modules，Nginx代理`/api/`剥前缀至后端。`frontend/src`按api/stores/pages/components/hooks/utils/router/constants/types拆分；`backend/src`按models/schemas/services/controllers/routes/middlewares/config/utils/constants拆分；`backend/scripts/seed_demo.py`自然键幂等且不覆盖用户修改。SQLAlchemy启动创建初始结构，迁移说明在backend/migrations。

后端开发：在backend安装requirements，设置DATABASE_URL/REDIS_URL/JWT_SECRET，`uvicorn src.main:app --reload`；前端可用Nginx或带同等代理的静态开发服务器。前后端全部业务计算在Python。

环境变量：COMPOSE_PROJECT_NAME、FRONTEND_PORT、BACKEND_PORT、DB_PORT、JWT_SECRET（部署替换）、DATABASE_URL、REDIS_URL、SEED_DEMO。`.env`不入Git。数据库/Redis/共享产物为命名卷。共享角色枚举admin/analyst/viewer与状态queued/running/completed/failed分别定义于backend/src/constants/enums.py和frontend/src/constants/enums.js。两个中间件：统一可恢复校验错误、每IP每分钟360请求限流。

## 因子勘误成组重述

当某个活动因子登记有误时，负责人可在“因子勘误”页选择**旧因子版本、新因子版本与受影响活动**发起勘误批次：

1. 系统只挑选**使用旧版本、包含受影响活动、已复核**的原报告，逐份复制其提交时冻结的来源快照与 SHA256 校验值；底稿后续修改不参与重算。
2. 每份原报告生成一条重述明细，worker 异步重算；原报告、原快照与已发出的站内交付全部保留，不被覆盖。
3. 整组重述必须全部完成，负责人填写至少 4 字复核说明后才进入 `reviewed`；任一明细失败则批次 `failed`，**旧组继续生效**，可重试（重试与重复投递幂等，不会重复生成报告或交付）。
4. 只有 `reviewed` 的批次才能**原子切换**其关联交付计划：逐行锁定计划、核对旧报告与代次，一次性把计划指向新组并递增代次；任何计划已被并发修改则拒绝“过时切换”，批次保持 `reviewed`。
5. 切换时已到期但尚未发出的结算补发为新组交付；已发出的交付保留原组。执行者持旧报告晚到写交付时会被计划行锁与代次校验拒绝。
6. 计算中断可恢复：worker 周期性把超时的 `running` 明细与未执行的 `queued` 明细重新入队。

新版本中除所选活动外的因子必须与旧版本一致，否则应拆分为多个勘误批次；重述报告只能随整批复核，不能单独复核。

## 已知边界
定时分发为站内收件箱；一名worker轮询执行，无外部邮件。调度发送已复核固定报告快照，不自动重算新数据。时间显示Asia/Shanghai。报告导出适合小型数据；底稿全量加载240行演示数据。Google Fonts不可用时回退serif。后端API以专属字段对象收发，页面通过业务输入控件编辑，不要求用户写JSON。新增来源期间自动可选；专属字段和算法严格校验。JWT演示有效期2小时。

MIT License。

## 实际实体映射
来源记录（Source，专属业务字段校验）、口径版本（Policy）、核算报告（Run）、交付计划（Schedule）、交付（Delivery）均经数据库→模型→请求schema→service→route/controller→frontend API→store→page。Period为已有来源期间字典，在任务期间选择器使用；不单独作为复杂度计数。底稿JSON为小型多字段单记录存储，不虚构未落库的子实体。

实际表：期间字典 activity_periods；专属来源 activities；冻结口径 emission_factors；异步报告 carbon_runs。
