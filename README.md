# varianttrail

合作商商品规格页面的 HTML 表单观察与授权请求留存。

## 现有能力

inspect 查看一份源文件的结构；capture 发起一次本机 HTTP 请求并保存响应状态、原始字节和摘要；show 列出接收记录。原文对象按 SHA-256 共用，独立请求分别记账。错误 HTTP 状态也会保存，原文归档不等于业务接收完成。

explore 在页面观察之上做**有边界的规格遍历**：只通过门户实际允许的 `/product`、`/select` 交互，从清空状态逐级重建选择路径，采集每个可达规格的 SKU、价格与库存，并解释哪些组合确实检查过、哪些因预算或条件不成立而没有 SKU。`explore-demo` 在本机门户演示“预算中断 → 进程重启用新令牌恢复 → 找回遗漏材质”的完整过程。

## explore 的边界规则

- 门户是有状态表单：每次选择消费一次性 token、重建下游选项并清空下游。explore 每深入一层都使用上一响应里的新 token，从 `/product` 重放路径，从不复用旧 token；检查点（`--state`）只保存 revision、frontier、叶子证据与冲突，**绝不保存 token**，进程重启后重新打开页面获取新令牌。
- 选项身份以稳定的 `data-key` 为准并按 key 排序入队；展示文案或选项排序变化不会制造新规格。`disabled`/`data-reason=condition` 的组合条件不成立、没有 SKU，记入 `blocked_edges`；`soldout` 仍可选择，SKU 保留并标注库存状态。
- SKU、价格只在**最终完整选择**页面绑定一次，中间选择页面不允许出现 SKU 串号。`single` 与 `gift` 合并到同一 SKU 时，两条选择路径的证据分别保留。
- `--budget` 限制本次运行的 GET 次数。预算用完即停，未走的分支原样留在 `unexplored` 并给出 `stopped_reason=budget_exhausted`，不会输出“采集完成”。
- 运行中商品 revision 变化会封存当前 epoch（旧 frontier 不并入新规则），在新 revision 下另开 epoch，两版规则不混用。
- 全程只做查询与选规格：非本机地址、`/purchase` 等写入路径在发起请求前即被拒绝，报告中 `write_actions_touched` 恒为空。

## 运行

使用 Python 3.11 或更新版本，仅依赖标准库，无安装步骤。全部示例均为本地合成资料，不包含真实个人信息、商业凭据或线上地址。

```sh
python3 -m varianttrail inspect varianttrail/fixtures/page.html
python3 -m varianttrail demo
python3 -m varianttrail explore http://127.0.0.1:8765 --budget 50
python3 -m varianttrail explore-demo
python3 -m unittest discover -s tests -v
python3 -m compileall -q varianttrail
```

`explore` 默认在当前目录写 `explore-state.json`（检查点）、`captures.sqlite3` 与 `objects/`，可用 `--state`、`--db`、`--objects` 指定独立位置。再次运行同一 `--state` 即从上次的 frontier 恢复；报告为标准输出 JSON，包含每个 revision epoch 的已检组合、SKU 分组（含多路径证据）、条件不成立边、未探索分支与请求开销。

需要手动发请求时运行 `python3 -m varianttrail.source_server --port 8765`，再在另一终端运行 `python3 -m varianttrail capture http://127.0.0.1:8765/sample`。默认会在当前目录创建 captures.sqlite3 和 objects；可通过 --db 和 --objects 指定独立位置。演示自己启动源服务，使用随机本机端口，并在退出时清理临时目录。

## 模块

`varianttrail/parser.py` 负责单文档观察，`explorer.py` 负责联动表单的有状态遍历、预算与修订边界，`archive.py` 保存不可变原文与请求事实，`transport.py` 限制实验请求到本机，`source_server.py` 提供来源示例。来源边界及字段含义见 `docs/source-contract.md`。这些入口不会声称一次响应已经构成完整目录或业务批次。

## 容器

Dockerfile 使用 Python 3.11 标准库镜像，不需要应用依赖下载。可用 `docker build -t varianttrail:local .` 构建，再执行 `docker run --rm --network none varianttrail:local`。本地 HTTP 与 PTY 演练不需要外部网络，测试及单次演示通常数秒完成。
