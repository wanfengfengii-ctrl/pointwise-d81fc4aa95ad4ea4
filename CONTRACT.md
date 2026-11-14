# 嵌套转换查询基线与重构交付契约

本项目将有序的多重集源表转换为有序结果，不连接数据库。
queryflow.py 是正确的旧执行器；samples.py 提供转换样例，checks.py 固定业务回归。protocol.py 仅提供目标执行核共用的 Fault 类型，boundary_checks.py 是尚待重构满足的接口探针。
queryflow.run(query, tables, params=None) 是既有 Python 调用入口。
python3 run.py 展示 samples.REQUEST 的真实结果；python3 run.py REQUEST.json 读取一个请求文件。
请求为 {"query":查询对象,"tables":源表映射,"params":参数映射}，params 可省略。
tables 的每项为 {"columns":[列名…],"rows":[行数组…]}，行长度等于列数，保持输入顺序，列名可重复。
数据值限 None/JSON null、布尔、整数及字符串；参数和 lit 也只接受这些标量。
请求树结构、操作名称和索引保证合法；源表缺失、列不存在、歧义、参数缺失和表达式运行错误属于可观察业务结果，不得提前拒绝。
查询对象图有限；Python 调用允许共享查询 dict、表达式 list 和经子查询调用形成的引用环，输入对象不能被修改。
递归用例的实际求值保证在基线正常完成；不要求判定不终止程序，不新增递归深度承诺。
文件 JSON 不表达对象共享或环，samples.shared 和 samples.recursive 展示 Python 入口的实际调用。
成功返回 {"columns":[输出名…],"rows":[行数组…],"trace":[事件…]}；业务异常返回 {"error":错误字符串,"trace":[此前事件…]}，不返回部分 rows。
适配入口只打印一行 JSON；对象字段排列不构成兼容要求，数组顺序和重复次数构成兼容要求。

## 已有查询格式与行为

查询含必填 select，其余键可省略：
* select 是 [输出名,表达式] 列表；输出名允许重复。
* from 是源项列表；省略即单个空行。源项是 {"table":表名,"as":别名} 或 {"query":查询对象,"as":别名}，均可附 "left":true。
* from 从左向右嵌套枚举。派生源是 LATERAL：每次使用之前源项的当前行以及外层环境执行，当前源自己的别名尚未入作用域。源无行且 left=true 时补一行该源声明宽度的 null。
* where 默认为 true，只保留严格布尔 true；不使用一般 Python 真值转换。
* 出现 group 键才聚合，键值是表达式列表。按行遇到顺序创建组，同组成员保持输入顺序；键由 queryflow.key 的标准 JSON 字节文本确定。group=[] 在输入为空时仍创建一个空组。
* having 默认为 true，仅在聚合查询应用；在 select 前执行。非聚合查询不执行 having。
* 聚合组中的普通列使用首个成员；空组使用所有当前源声明列都为 null 的合成环境。外层环境保持不变。
* distinct=true 按 queryflow.key 的文本去重，保留首行。
* order 是 [从0计的输出列索引,是否降序] 列表；最后一项先作稳定排序。升序按 (值不是null, Python类型名, 值) 排，降序反向。
* limit 为非负整数，省略不截断；limit=0 在任何源读取、表达式求值前返回空。未排序时达到 limit 即停止拉取；order 则先完成全部投影和排序。

表达式是下列列表：
* ["lit",值]；["param",参数名]；["col",列名]；["col",别名,列名]。
* ["if",条件,真支,其他支]；["coalesce",表达式…]；["not",表达式]。
* ["op",操作,左,右]，操作为 eq、lt、add、div、and、or。
* ["scalar",查询]；["exists",查询]；["in",左表达式,查询]。
* ["agg",操作,表达式]，操作为 count、sum、min。

查列从最近环境帧开始。非限定列只在该帧具有同名列时停止搜索，多个匹配报 AMBIGUOUS:列名。
限定列只要该帧出现别名就停止搜索：该别名有多个绑定报 AMBIGUOUS:别名.列名，单个绑定缺列报 UNKNOWN:别名.列名，重复列也报 AMBIGUOUS；不得退回同别名外层。
找不到任何匹配报 UNKNOWN:引用文本，缺参数报 PARAM:参数名。派生源返回的重复输出名也参与歧义判断。
and/or/not/if 条件只接受布尔或 null，否则 BOOL_REQUIRED；null 为未知。
and 的 false、or 的 true 均短路；未知左值仍求右值，false and unknown=false，true or unknown=true，其余未知传播。
if 仅 true 选真支，false/null 选其他支；coalesce 只求值到首个非 null。
其他二元运算左后右；任一为 null 则为 null，但右操作数仍被求值。eq 用 Python 标量相等，lt 用 Python 比较，不可比报 COMPARISON。
add/div 只接受精确 int 类型，否则 INT_REQUIRED；div 为向下整除，零除报 DIV_ZERO。
每次执行表源先追加 ["scan",表名]；即使缺表也先记事件再报 TABLE:表名。行内容不记入 trace。
执行 scalar/exists/in 时先追加 ["subquery",种类]；in 的左表达式在追加事件前求值。
scalar/in 要求恰一输出列，否则在读源前报 SUBQUERY_WIDTH；exists 不检查宽度。
scalar 空结果为 null；拉到第二行才报 SCALAR_MANY，第二行投影本身出错时先报该错，不读第三行。
in 按源序读取，匹配即停止；无匹配但发生 null 比较返回 null，空结果始终 false。
exists 只检查有无通过 where/having 的候选，跳过 select、distinct、order，但保留 limit=0 的抑制。
agg 使用当前查询的组，不能捕获外层组；无当前组报 NO_GROUP。参数表达式按每个成员环境求值，忽略 null。
count 返回非 null 数量；sum 要求精确整数，空集合为 null；min 用 Python 最小值，不可比报 COMPARISON，空集合为 null。
同一查询直接嵌套 agg 报 NESTED_AGG；进入子查询后聚合上下文重新建立。所有错误及错误前 trace 的精确先后保持 queryflow.py 的既有行为。

样例 ENRICH 输出 [["A",12],["A",12],["B",null],["C",5]]。
样例 TOTALS 输出 [["A",2,24],["B",0,null],["C",1,5]]，列为 ["site","n","total"]。
samples.shared(18) 的输入含55个互异查询对象，存在2的18次方条词法调用路径；一次执行仅选择一条，默认及反转交替参数均为 [[27]]，全true为 [[18]]，全false为 [[36]]。

samples.recursive 的4个查询对象含调用环，输入 n=[0,3,6] 输出 [[0],[6],[21]]；以横向派生源逐轮把 n 减一，再累计 1 到原 n。

## 本轮结构交付

只重构职责与依赖边界，不新增查询语义或优化策略。
保留 queryflow.run(query,tables,params=None)；queryflow.py 最终只保留导入和兼容 run 转发，不含解释器或备用执行路径。
新增 exprkernel.evaluate(e,env,params,group=None,aggregating=False)、relationkernel.execute(q,outer,tables,existence=False) 和 coordinator.run(query,tables,params=None)。两个核均返回 Python 生成器；启动用 next，响应用 send，业务失败用 throw(protocol.Fault(message)) 注入。
env 是从近到远的帧列表；帧是绑定列表，绑定为 (alias,names,row)。group 为 None 或成员 env 列表；空列表表示存在空组。outer 使用同一种帧列表。核不得修改查询图、源表、参数、环境或组。

表达式核独占名称查找、标量运算、短路、聚合参数求值及子查询消费规则；不能接收 tables、读取表、创建或调用关系执行器。
表达式生成器通过 StopIteration.value 返回标量，只能发出以下请求：
* ("trace",event)：响应 None。子查询事件由表达式核在旧位置发出。
* ("open",query,outer,existence)：响应一个不透明关系句柄。只建立关系生成器，不能推进或读源；不传递调用者的 group 或 aggregating。
* ("next",handle)：响应 ("row",row) 或 ("end",)。失败通过 throw 注入；不能返回异常对象代替失败。
* ("close",handle)：响应 None，关闭该关系及其尚未完成的后代，不推进读取。每次成功 open 必须恰好 close 一次，正常结束、提前命中及错误均如此；关闭不新增业务 trace。
句柄不提供迭代接口，表达式核只能原样携带，不读取或解释它。

关系核独占表源读取、横向源枚举、外关联补空、分组、投影顺序、去重、排序和 limit；不能求值任何表达式，包括字面量、列和聚合。where/having 返回值的布尔类型检查与过滤由关系核负责。
关系生成器只发出以下事件/请求：
* ("eval",e,env,group,aggregating)：响应表达式标量；失败通过 throw 注入。新查询从无组上下文开始；where、group 键与普通投影分别沿用旧代码传参，having/聚合投影带本地组。
* ("trace",event)：响应 None；表扫描事件由关系核在旧位置发出。
* ("row",row)：交付一条最终结果行；协调器只有在再次拉取时才 send(None) 继续。
关系生成器返回 None 表示结束，不能将内部横向源的行当成外层结果交付。允许关系核内部递归复用自身，但所有表达式均须经过 eval 请求。

协调器独占活生成器、句柄、请求路由、异常注入和 trace 收集；响应 eval 时调用表达式核，响应 open/next/close 时管理关系核。最终拼装既有结果或错误；可读取顶层 select 的输出名，除此以外不解释查询键、表达式标签、操作或行值。
两个执行核不得导入或调用彼此、coordinator、queryflow 或样例；只依赖标准库及 protocol。protocol 只放无执行逻辑的数据/异常定义，不承载规则。coordinator 通过模块属性 exprkernel.evaluate 和 relationkernel.execute 调用，支持替换任一核验证路由；不能另藏解释器或回调执行器绕开请求接口。
不得预先物化子查询、按调用路径展开共享/递归查询图或把旧解释器保留为备用；旧代码本来需要的分组、排序物化保持不变。不要求编译器、数据化计划、暂停落盘、并发、性能提速或新语法。

## 验证与交付入口

python3 checks.py 核对现有业务，必须退出0并输出 BUSINESS_REGRESSION_OK。
python3 run.py 展示 TOTALS，python3 checks.py demo 展示真实转换、共享调用及递归调用结果。
python3 boundary_checks.py business 只核对附加业务边界，现有基线须退出0并输出 ADDITIONAL_BUSINESS_OK。
python3 boundary_checks.py 是重构后的执行协议与独立核探针，须退出0并输出 BOUNDARY_REGRESSION_OK；当前缺少目标模块属于未完成的结构目标，不是业务故障。
保留 checks.py 的全部既有断言；完成后让其默认入口同时运行原业务回归和 boundary_checks.regression，运行清单 verify 保持 python3 checks.py。
结构验收结合探针和源码：两个执行核能在对方及协调器禁止导入时运行，表达式核能使用脚本化关系响应，关系核能使用不解释表达式的替身响应；协调器替换任一核后仍走相同路由。
完整入口还须保持聚合成员相关子查询、空组、短路、scalar 第二行错误、exists 跳过投影、in 提前命中和递归样例的结果、顺序、重复次数及完整 trace；输入对象及其共享关系不变。
交付 RESPONSIBILITIES.md，说明上下文、拉取和关闭的归属，附实际回归结果。接口探针不代替对依赖方向、规则归属及无备用路径的源码复核。
