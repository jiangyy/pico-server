# PicoPG

Pico 的服务端。客户端与服务端之间只交换不可变的记录（Replica Log）；草稿、提交、评语、课程目录都由这些记录投影出来。

## 目录

```
src/picopg/
  replica/              Log 契约（log.py）与文件存储（file_store.py）；不认识任何业务
  server/
    __main__.py         python -m picopg.server：命令行入口
    config.py           账号、绑定、提交仓库、令牌与 AI 的配置读取
    app.py              装配：Log 授权规则、各领域接口、后台 worker
    replication.py      HTTP 基类与 Replica 协议（握手、读写 Log、SSE 事件）
    common/             各领域共用：Log id 编码、分页读取、参数校验、worker 线程
    auth/               访问码登录、会话与设备、按 Principal 的 Log 授权
    playgrounds/        课程包发布、目录与下载
    drafts/             多设备草稿：协议、裁决（authority）、worker
    submissions/        提交：协议、worker、Git tag、读取与学生端接口
    review/             评语，以及运维、教师查看提交的接口
    classroom/          班级名册
    ai/                 课程 AI、agent 与全局导师；model.py 是 OpenAI 兼容客户端，skills/ 是课程要点
    analysis.py         学习分析 Log 的授权规则
tests/                  unittest，每个领域一个 test_*.py
deploy/                 单机部署：systemd、nginx、备份、上线冒烟
benchmarks/             文件存储基准
```

## 运行

```sh
PYTHONPATH=src python3 -m picopg.server --data data --insecure-cookies
```

常用参数见 `--help`。课程 AI、agent 与导师需要环境变量 `PICOPG_AI_BASE_URL`、`PICOPG_AI_MODEL`、`PICOPG_AI_API_KEY`（可选 `PICOPG_AI_EFFORT`）。本地联调一般不直接运行，而是在 learn-python 里执行 `npm run dev:stack`。

## 测试

```sh
./run-tests.sh                          # 全部
./run-tests.sh tests.test_replication   # 一个文件
```

## 运维

部署步骤、冒烟与备份见 [deploy/README.md](deploy/README.md)。

- **nginx 必须允许大 body**：课程包上传可达几十 MB，`deploy/nginx-picopg.conf` 设置了 `client_max_body_size 80m`；改用别的反向代理时要同样放开，否则上传会被代理以 413 拒绝。
- systemd 单元以 `python3 -m picopg.server` 启动，`PYTHONPATH` 指向仓库的 `src/`；从旧版（`apps/server/main.py`）升级时要重新拷贝 `deploy/picopg.service` 并执行 `systemctl --user daemon-reload`。
