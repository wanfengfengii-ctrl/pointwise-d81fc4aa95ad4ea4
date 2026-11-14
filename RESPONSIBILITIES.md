# 职责与依赖边界说明

本轮只做职责拆分，不新增查询语义。旧 `queryflow.py` 的融合解释器被拆为两个执行核加一个协调器，`queryflow.run(query, tables, params=None)` 保留为兼容转发。

## 模块与依赖方向

```
queryflow.py      仅导入 coordinator 并转发 run（无解释器、无备用路径）
coordinator.py    路由层：import exprkernel / relationkernel，经模块属性调用
exprkernel.py     表达式核：仅依赖标准库与 protocol
relationkernel.py 关系核：仅依赖标准库（json）与 protocol
protocol.py       仅 Fault 异常定义，不承载规则
```

两个核互不导入，也不导入 coordinator、queryflow、samples；协调器不含任何表达式或关系规则，只通过 `exprkernel.evaluate` 与 `relationkernel.execute` 模块属性调用，任一核可用替身替换而路由不变。

## 上下文归属

- **表达式核**独占：名称查找（限定/非限定列、歧义与 UNKNOWN）、标量运算、and/or/not/if/coalesce 短路、聚合参数求值，以及 scalar/exists/in 的子查询消费规则（宽度检查、第二行基数检查、命中即停、null 传播）。
- **关系核**独占：表源读取、横向源枚举（含空前缀）、left 补空、分组与空组合成环境、投影顺序、distinct、排序、limit；where/having 返回值的布尔类型检查与过滤也在此完成。
- **上下文不串用**：`("open", query, outer, existence)` 只携带外层 env，不携带调用方的 group/aggregating；新查询的 eval 从无组上下文重新开始。聚合参数按每个成员 env 以 `(member, None, True)` 求值，相关子查询跟随成员而非代表行。协调器不解释查询键、表达式标签、操作或行值，只读取顶层 select 的输出名拼装结果。

## 拉取归属

- 表达式生成器通过 `StopIteration.value` 返回标量；只发出 trace/open/next/close 四种请求。open 只建立关系生成器，不推进、不读源。
- 关系生成器只发出 eval/trace/row 三种请求；`("row", row)` 交付一条最终结果行，协调器再次拉取时才 `send(None)` 继续。横向源的子关系行由关系核内部消费，不作为外层结果交付。
- 协调器独占活生成器与请求路由：响应 eval 时驱动一个新的表达式生成器；响应 next 时按句柄拉取关系生成器一行（或 end）；子查询不预先物化，共享/递归查询图不展开，分组与排序的既有物化保持不变。
- 错误注入：关系侧失败经 `throw(Fault)` 注入表达式生成器的 next 点；表达式失败经 `throw(Fault)` 注入关系生成器的 eval 点；因此 scalar 第二行投影错先于 SCALAR_MANY、exists 跳过投影、in 命中即停、错误前 trace 顺序均与旧实现一致。

## 关闭归属

- 每次成功 open 由表达式核恰好 close 一次：正常结束、in 提前命中、scalar 基数拒绝以及 next 注入错误（finally 中发出 close）都覆盖；close 不推进读取、不新增业务 trace。
- 协调器独占句柄生命周期：句柄是不透明 object，按打开时的属主栈登记父子关系；close 关闭该关系及其尚未完成的后代（`gen.close()`，不推进）。表达式核只原样携带句柄，不读取不解释。

## 回归结果（本机实际运行）

```
$ python3 checks.py            # 业务回归 + boundary_checks.regression
BUSINESS_REGRESSION_OK
ADDITIONAL_BUSINESS_OK
BOUNDARY_REGRESSION_OK
$ python3 boundary_checks.py   # 结构探针（隔离导入、双核替身、协调器路由）
ADDITIONAL_BUSINESS_OK
BOUNDARY_REGRESSION_OK
$ python3 boundary_checks.py business
ADDITIONAL_BUSINESS_OK
$ python3 run.py               # TOTALS
{"columns":["site","n","total"],"rows":[["A",2,24],["B",0,null],["C",1,5]],"trace":[["scan","readings"],["scan","calibration"],["scan","calibration"],["scan","calibration"],["scan","calibration"]]}
$ python3 checks.py demo       # ENRICH/TOTALS、shared(8)、recursive 均与基线一致
```

以上命令退出码均为 0；checks.py 的全部既有断言原样保留，shared(18) 与递归样例的结果、顺序、重复次数及完整 trace 经回归核对，输入对象图在运行前后不变。
