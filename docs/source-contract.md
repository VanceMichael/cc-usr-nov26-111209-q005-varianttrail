# 联动规格门户

门户是普通 HTML 表单，所有选择动作均为 GET。/product 新建一个有独立一次性 token 的观察会话。每个页面包含 revision、当前选择、一个查询 form，以及独立的 /purchase 写入按钮（禁止使用）。/select?token=...&field=material&value=cotton 选择字段后返回新 HTML 与新 token，旧 token 再用返回 409。可选字段顺序 material、color、pack；改变上游选择会清空下游，无法跳过尚未满足的依赖。值只是当前页面上下文中的选项值，稳定 option key 放在 data-key 中。

棉的颜色有白和黑；麻只有原色；麻的黑为 disabled 且 data-reason=condition。小包装总能查，棉白的大包装显示 soldout 但仍有 SKU。pack=single 与 pack=gift 对某些材质会给出相同 SKU。完整选择后 #sku 给出 data-id、data-price 和 data-stock。没有完整选择不提供SKU。disabled 不允许选择，但 soldout 选项允许选择。/control/phase?value=updated 改变商品 revision、展示排序和一种包装，既有 token 请求返回409，需要重新打开 /product。

source_server 的单个进程保存会话并锁定 token 消费；重启清空会话。此门户只含授权合成商品，不需要浏览器；可以用 HTMLParser、cookie/token 状态和 HTTP 完成实际页面交互。数据观察和下单路径明确分开，/purchase 始终拒绝演练客户端。
