# DESIGN.md — 撤链后局部采集资格维护设计（只设计，不实现局部维护器）

本文件是本轮唯一新增的设计交付物；业务源码、录制与断言保持不变，运行清单
（`.pointwise-runtime.json`）仅按约定把 verify 接到文档核验。文中所有“实测”
均来自当前全图实现 `python3 -B evidence.py observe` 的录制；所有“预测”都是
拟议局部算法在**尚未实现**情况下的纸面推演，核验脚本只重放实测与状态一致性，
不代替对支撑判据、事务顺序和访问上界的人工审查（evidence.py:68）。

## 1. 现状链路：claim、complete 与 Store 事务如何串起全图重算

### 1.1 存储结构与事务边界

`Store` 在内存 SQLite 中持有四张表（store.py:10-21）：

- `page(url, eligible, generation, pending)`：每页一行，资格、代次、待办标志
  同行存储（store.py:12-14）；
- `seed`：固定种子表，构造后不再变化（store.py:15）；
- `edge(src,dst)`，主键 `(src,dst)` 保证重复边折叠，另有 `edge_by_dst(dst,src)`
  反向索引（store.py:16-18）；
- `lease(token,url,generation)`，`url UNIQUE` 使同页不可能同时存在两个活动令牌
  （store.py:19-20）。

所有写操作都经过 `Store.write()` 上下文（store.py:30-42）：最外层开启
`BEGIN IMMEDIATE`，正常退出提交，任何 `BaseException` 都触发 `ROLLBACK` 并重新
抛出；嵌套调用复用同一事务、不设保存点。这正是外层回滚能整体恢复出链、新页面、
资格、代次、待办与令牌的机制；进程内令牌编号（crawler.py:9 的 `count(1)`）与
读取记录 `trace` 不参与数据库状态，按约定不回滚。

`replace(src, targets)` 先删该页全部旧边，再对去重排序后的新目标逐边
`ensure`+插入（store.py:47-52）；`ensure` 用 `INSERT OR IGNORE` 发现新 URL
（store.py:44-45）。逻辑读取口径在 store.py:54-81：`all_graph` 计全表行数，
`outgoing`/`incoming` 各计一次邻接查询（空查询也计），`page` 是资格/代次/待办
点读，`lease` 是令牌查找；`inspect` 的诊断读不入 trace（store.py:83-93）。

### 1.2 初始化与全图重算

`Crawler.__init__` 在一个事务内建库并调用 `_rebuild()`，随后清空 trace，使初始化
的全扫不计入运行期观察（crawler.py:7-12）。`_rebuild()`（crawler.py:14-34）：

1. `all_graph()` 读出**全部**页面与边，在进程内拼邻接表（crawler.py:15-18）；
2. 以种子集合为起点做 DFS 求种子可达集 `reached`（crawler.py:19-25）；
3. 对**每个**页面点读旧状态（crawler.py:27），资格翻转则写新值并把代次加一
   （crawler.py:29-32），新失格页同时删除其 lease（crawler.py:33-34）。

### 1.3 claim 路径

`claim`（crawler.py:36-52）整体在一个写事务内：先数活动令牌，达到 2 个直接
返回 None（crawler.py:38）；无指定 URL 时取字典序最小的 pending 页，指定时只
查该页（crawler.py:40-45）；命中后领取即把 `pending` 置 0 并插入带当前代次的
lease（crawler.py:49-51）。claim 不改变图，不触发任何资格维护读取（录制中
claim 步骤的 `reads` 均为 `[]`），局部方案无需改动它。

### 1.4 complete 路径（当前实现）

`complete`（crawler.py:54-71）同在一个写事务内顺序完成：

1. `lease(token)` 查找；查无令牌先返回 `stale`，且不碰 links（crawler.py:56-58）；
2. 点读本页，资格已失或代次与令牌不一致也返回 `stale`（crawler.py:60-62）——
   恢复资格产生新代次，旧任务因此不可能被重新接纳；
3. links 校验失败抛 `ValueError("BAD_LINKS")`，此刻尚未写任何数据，异常经
   `write()` 传播，原状态与令牌保留（crawler.py:63-65）；
4. 删除本令牌、`replace` 原子替换全部出链（crawler.py:66-67）；
5. 调用 `_rebuild()` 全图重算资格、待办与受影响令牌（crawler.py:68）；
6. 本页仍合格则重新进入待办，开始下一轮巡采（crawler.py:69-70）。

每次 `complete` 返回后状态即与当前已知图的种子可达性一致，而不是最终才收敛。
外层再包一层 `write()` 并主动抛异常时（samples.py:48-56 的 rollback 录制），
第 4-6 步的全部写入随 ROLLBACK 撤销，事务内 `inspect` 看到的临时新页面、新边和
资格变化在回滚后消失，但已消耗的令牌编号不复用。

## 2. 全图方案的实测成本（基线，非故障）

`growth(0)` 与 `growth(1000)` 都只让 `a` 完成空出链，后者仅给种子 `s` 增加一条
`u0…u999` 的独立千页链（samples.py:65-72）。observe 实测（见文末 actual）：

| size | all_pages | all_edges | page 点读 | lease 查找 |
|---:|---:|---:|---:|---:|
| 0 | 5 | 5 | 6 | 1 |
| 1000 | 1005 | 1005 | 1006 | 1 |

一次与 `a,b,c,d` 无关的边缘分支增长，让维护读取从 5 线性涨到 1005：全图重算的
时间为 O(V+E)，且每次完成都付全量代价，与变化大小无关。checks.py:84-87 把这一
增长固化为回归断言；checks.py:6-13 的独立松弛 oracle 与 12 组随机小图
（checks.py:68-83）保证全图基线本身正确，可作为局部方案的语义对照。

## 3. 拟议局部组织：状态、影响区与入边边界

### 3.1 维护的状态不变量

数据库状态与现状完全相同（page/seed/edge/lease 四表），不新增表、不做全表
缓存。待办恒等于“合格且无活动令牌”，代次在资格真假每次切换时加一，初始为 0、
首次取得资格为 1。局部维护只改变**资格可能翻转的页面**，其余页面一行不写。

### 3.2 变化边与前向影响区 D

设一次有效完成前、后图为 G、G'，只有完成页 u 的出链变化。变化边
Δ = (out_G(u) △ out_G'(u))，其目标集合 T = {v : (u,v) ∈ Δ}；Δ 为空时 D 为空，
维护只做令牌与本页待办更新。影响区 D 是从 T 出发、在**并图 G∪G'** 中前向可达的
全部顶点（含 T）。构造方法（只允许局部邻接访问）：

```
D ← ∅ ; work ← 去重(T)
old ← outgoing(u)          # 换边前读一次，代表 G 中 u 的邻接
# new 即 complete 入参去重结果，已在内存，无需查询
while work 非空:
    v ← work.pop()
    if v ∈ D: continue
    D.add(v)
    if v = u:
        nbrs ← old ∪ new    # 并图中 u 的邻接，复用已读 old，不再查库
    else:
        nbrs ← outgoing(v)  # 只有 u 的边被改，D 内其他点 G/G' 邻接相同
    work.push(nbrs 中不在 D 的点)
```

u 本身只有在变化边能绕回 u（含自环、经环回流）时才进入 D。并图而不是 G' 上
闭包，保证“因删边而即将失格”的页面（如被断开的环）仍被纳入 D，不漏停。

### 3.3 入边边界 B 与支撑判据

边界只在 **G'** 上定义：B = {(x,v) ∈ E(G') : v ∈ D, x ∉ D}。对每个 v ∈ D 用
反向索引做一次 `incoming(v)`（store.py:66-70 走 edge_by_dst），筛出源在 D 外的
边即得 B；对每个边界来源 x 只做一次 `page(x)` 点读取资格标志，**绝不展开 x 的
出链**，因此来源侧的其他分支不会被访问。

支撑判据（局部可达性的核心）：

```
supported ← ∅
# 种子支撑：D 内的种子自身合格（固定种子，内存集合 O(1) 判断）
for v ∈ D ∩ seeds: supported.add(v)
# 边界支撑：来源在 G' 中合格的边界边把可达性注入 D
for (x,v) ∈ B:
    if page(x).eligible: supported.add(v)
# 在 G' 上沿 D 内出链传播支撑（邻接已在构造 D 时缓存，无需再次查库）
work ← list(supported)
while work 非空:
    v ← work.pop()
    for w ∈ out_G'(v):           # u 用 new，其余点用已缓存邻接
        if w ∈ D 且 w ∉ supported:
            supported.add(w); work.push(w)
```

结论：v ∈ D 在 G' 中合格 ⇔ v ∈ supported。

**为什么孤立环不能自保（不能拿环内旧资格当外部支撑）**：种子可达性要求存在一条
从种子出发的有向路径。任何从 D 外进入 D 的路径都必须经过某条边界边 B；D 内的边
（含 c↔d 这类环）只能在 D 内部绕行，把各点的“可达性”互相指认为彼此的前提，构成
循环论证。旧 `eligible` 标志是按 G 计算的，可能恰好依赖那条被删除的入口边，因此
环内旧资格不能作为支撑源。把图缩成 SCC condensation 后结论同样显然：D 的入口
SCC 若没有种子、也没有来自合格外部 SCC 的边界边，整个 SCC 及其后继都不可达。
shared 第二步 c、d 因 b→c 边界仍在而存活，第五步边界消失后互链旧边仍在
（CONTRACT 要求失格不删边）却必须同时失格，就是该判据的两端。

### 3.4 资格、代次、待办与令牌的同事务更新

对 D 内每页点读一次旧状态，按 supported 集合翻转：

- 合格→失格：`eligible=0`、`generation+=1`、`pending=0`，并删除该页 lease
  （活动令牌立即失效）；
- 失格→合格：`eligible=1`、`generation+=1`、`pending=1`（新代次，旧令牌早已在
  失格时删除，天然无法回流）；
- 资格不变：一行不写，其活动令牌保持有效，禁止整批失效。

完成页 u 无论是否在 D 中，最后统一执行一次
`UPDATE page SET pending=1 WHERE url=u AND eligible=1`（与 crawler.py:69-70 同形），
实现完成即进入下一轮待办。D 外页面不读不写，其令牌与待办原样保留。

## 4. 关键事务伪代码

### 4.1 claim（与现状完全相同，无图变更、无影响区工作）

```
with store.write():
    if count(lease) >= 2: return None
    row = 选 pending 页（字典序最小或指定 URL）   # crawler.py:40-45
    if row is None: return None
    token = "t" + next(serial)                     # 编号永不复用，回滚也不复用
    UPDATE page SET pending=0 WHERE url=row.url
    INSERT lease(token, row.url, row.generation)
    return {url, token}
```

### 4.2 complete 的局部维护事务（拟议，替换 crawler.py:66-70 的全图段）

```
with store.write():
    L = lease(token)                                 # 一次 lease 查找
    if L is None: return "stale"                     # 未知/重复/已失效：忽略 links
    (u, g0) = L
    (elig, gen, pend) = page(u)                      # 一次点读
    if not elig or gen != g0: return "stale"         # 旧代次任务不接纳
    if links 非 list 或含非空字符串以外元素:
        raise ValueError("BAD_LINKS")                # 写前校验，状态与令牌保留
    DELETE lease WHERE token=token
    old = outgoing(u)                                # 换边前邻接，一次 out 查询
    new = sorted(set(links))
    T = (set(old) ^ set(new))                        # 变化边目标
    if T:
        D = union_forward_closure(T, old, new)       # 见 3.2，只查 D 内邻接
        B = []
        for v in sorted(D):                          # 每个 D 点一次 in 查询
            for x in incoming(v):
                if x not in D: B.append((x, v))
        src_ok = {x: page(x).eligible for (x, v) in B}   # 边界来源点读，不展开
        supported = propagate(D, B, src_ok, seeds)   # 见 3.3，传播只用缓存邻接
        for v in sorted(D):
            st = page(v)                             # D 内每点一次点读
            want = int(v in supported)
            if want != st.eligible:
                UPDATE page SET eligible=want,
                       generation=st.generation+1,
                       pending=want WHERE url=v
                if not want: DELETE lease WHERE url=v
    replace(u, new)                                  # 原子换边，顺带发现新页
    UPDATE page SET pending=1 WHERE url=u AND eligible=1
    return "accepted"
```

顺序要点：旧邻接与 D/B/supported 全部在**换边前**基于“内存中的新边 + 库中的
旧图”算出，`replace` 落在事务后半段，因此邻接查询不会读到半新半旧的图；新发现
页面在 `replace` 时以默认 `eligible=0` 建行，若它 ∈ D（新增边目标必在 D 中），
随后的翻转阶段把它置为合格、代次 1、进待办，若不在支撑集则保持失格。所有动作在
同一 `write()` 事务内，提交前对外不可见。

### 4.3 stale、BAD_LINKS 与外层回滚

- **stale**：在任何写操作之前返回，唯一维护读取是一次 `lease` 查找；代次不符时
  再加一次本页 `page` 点读。不构造 D、不查邻接、不发现页面（CONTRACT：stale 步骤
  无图维护读取，文末预测中其 region/boundary 均为空）。
- **BAD_LINKS**：校验先于删令牌与换边，抛出后 `write()` 回滚，本就未写任何内容，
  令牌仍可被下一次合法 complete 接纳（checks.py:39-45 覆盖）。
- **外层回滚**：complete 嵌套在外层 `write()` 中时，换边、新页、资格翻转、代次、
  待办与 lease 变更全部属于同一数据库事务，ROLLBACK 后与领取后状态逐字节一致
  （checks.py:30-34）；serial 与 trace 按约定不回滚，故重试拿到的是新编号。

## 5. 正确性论证：为何不漏停、不误停

1. **D 外资格不变**：G 与 G' 仅在 u 的出链上不同。任何路径若用到变化边，其必经过
   某目标 v ∈ T 并此后停留在 T 的前向闭包 D 内；D 外页面之间的边在两图中完全一致，
   种子到 D 外页面的可达性不依赖变化边，状态保持不变，故不需要也不允许访问它们。
2. **D 内判定完备**：G' 中种子到 v ∈ D 的路径要么完全在 D 内且起点是 D 内种子，
   要么从 D 外经唯一入口——某条边界边 (x,v)——进入，且 x 在 G' 中合格。支撑传播
   穷尽这两类入口及 D 内全部 G' 出边，supported 恰为 G' 的 D 内可达集，与全图 DFS
   （crawler.py:19-25）在 D 上的结果一致。
3. **不漏停**：删边只能消灭可达性。并图闭包保证所有“可能因本次变化失去入口”的点
   都在 D 中；失去边界支撑的环（shared 第五步、epoch 断链）在 supported 中没有
   入口，必然翻转为失格并同步删令牌、清待办。
4. **不误停**：仍有合格外部入口的页面（shared 第一步经 b→c、恢复链 epoch 经
   s→a）一定被边界注入支撑并沿 G' 边传播到全部后继，资格不动、令牌不失效。
5. **代次隔离**：失格即删 lease 且代次加一；恢复资格再加一代，旧令牌的代次永远
   落后，complete 入口的双重检查（crawler.py:60-62）保证 poison 一类旧结果不可能
   入图（epoch 第五、六步实测）。
6. **事务原子性**：资格、待办、令牌、链接替换同事务提交或一起回滚，每次返回后
   都与当前已知图可达性一致，不存在中间态外泄。

## 6. 成本（时间、空间、读写与索引，单列）

记 D 的顶点数为 d，D 内页面在并图中的出边总数为 e_D，边界边数为 b，u 旧/新出度
为 k_old、k_new，翻转页数为 f（f ≤ d），新失格页数为 q。

**逻辑读取（对应 trace 口径，初始化后）**

- out 邻接：u 一次（换边前），D 内每页一次；u ∈ D 时其闭包展开复用该次缓存不另查，
  故 u∉D 时共 d+1 次、u∈D 时共 d 次；变化边为空时不构造 D，仅 u 的 1 次且无后续维护；
- in 邻接：D 内每页一次反向索引查询，共 d 次；
- page 点读：D 内 d 次 + 边界来源去重后至多 b 次 + complete 入口对 u 的 1 次；
- lease 查找：每次 complete 1 次；stale 仅付这一次（代次不符再加 1 次 page）。

**写与 B 树路径（trace 不计量，须单列）**

- 边替换：k_old 次删除 + k_new 次插入，每次同时维护 edge 主键与 edge_by_dst 两条
  B 树路径，O((k_old+k_new) log E)；新页插入 O(log V)；
- 状态写：仅 f 个翻转页各一次 page 行写，O(f log V)；lease 删除至多 q 次；
- 不变页、无关分支零写入。

**进程内时间与空间**：闭包与传播合计 O(d + e_D + k_old + k_new + b)；内存只保存
D 集合、其邻接缓存与边界，O(d + e_D)，不复制全图、不做全局缓存刷新。最坏情况
D 覆盖全图（d=V, e_D=E），退化到与全图重算相同的 O(V+E) 渐近上界，不会更差；
growth 场景下 d=2 且与千页链无关，成本恒定。

**现有索引能做什么、不能做什么**：edge 主键让 outgoing(u) 是按 src 的范围查询
（store.py:60-64），edge_by_dst 让 incoming(v) 免全边扫描（store.py:66-70），
lease 的 url UNIQUE 与 token 主键分别支撑同页互斥与按令牌查找。索引不能判定可达
性：它只定位边，“环内互链是否构成种子可达”仍须由 3.3 的支撑传播回答；索引维护
本身也是每次换边必付的写放大，不能借重建索引之名行全表扫描之实。

对照全图基线：当前每次完成固定付 all_pages/all_edges 两次全扫（store.py:54-58）
加 V 次 page 点读（crawler.py:26-27），实测 V=5→1005 时线性膨胀；局部方案把这笔
代价替换为只随 d、e_D、b 增长的局部访问。

## 7. 三个样例的逐步预测（对照文末 JSON，actual 为实测、predicted 为推演）

region 为并图前向闭包 D（字典序），boundary 为 G' 上进入 D 的入边（按 src,dst
排序）。eligible/pending/active 三列直接取自该步实测 state，verify 会逐项重放
（evidence.py:45-49）；region/boundary/why 是拟议方案的预测。

### 7.1 shared（s→a,b；a,b→c；c↔d）

- **步骤 3，complete A=[]（删 a→c）**：D={c,d}（并图中 c↔d 闭环）；边界仅
  b→c，来源 b 合格；支撑注入 c 后传到 d，c、d 全部保住资格，c 的 t1 令牌有效，
  待办只在 a 上摘除再重挂。不漏停（d 经 c 间接存活）也不误停。
- **步骤 5，complete B=[]（删 b→c）**：D={c,d}；G' 中进入 D 的边界为空，c↔d
  成为孤立环，无种子、无合格外部来源，supported 为空；两页同时失格、代次加一、
  待办移除、t1 立即失效；互链旧边按契约保留（state.links 仍含 c→d、d→c）。

### 7.2 epoch（s→a→c↔d，先断链再接回）

- **步骤 3，complete CUT=[]（s 删唯一出边 a）**：T={a}，并图闭包 D={a,c,d}；
  G' 中无任何从 D 外进入 D 的边，边界为空，三页全部失格，c 的旧令牌 t1 随翻转变
  失效并删除；s 在 D 外，资格与待办不变。
- **步骤 5，complete OPEN=[a]（s 接回 a）**：T={a}，D={a,c,d}；边界 s→a，来源
  s 合格，支撑依次传播 a→c→d，三页以**新代次**恢复并进待办；此时 lease 表为空。
- **步骤 6，complete OLD=[poison]（stale）**：t1 已在步骤 3 删除，一次 lease
  查找即返回 stale，无 D、无邻接读、无任何写入，poison 不入图，state 与步骤 5
  完全相同（实测断言 checks.py:27-28）。
- **步骤 8，complete NEW=[]（c 删 c→d）**：T={d}，并图保留旧边 d→c、c→d，故
  D={c,d}；G' 边界为 a→c（a 合格），支撑到达 c 后因 c 已无出边而止于 c；c 保资格
  并重挂待办，d 失去唯一支撑而失格、代次加一。

### 7.3 rollback（领取 a 后换链到新页 new，再外层回滚）

- **步骤 2，rollback A=[new]（尝试 a: [c]→[new]）**：T={c,new}，并图闭包
  D={c,d,new}；G' 边界为 a→new、b→c，来源 a、b 均合格，supported={c,d,new}，
  事务内可见 new 取得资格并进待办（result.inside 实测）；外层 ROLLBACK 后链接、
  新页、资格、代次、待办、令牌全部恢复到领取后状态，t1 仍有效。region/boundary
  描述的是被撤销的尝试，状态列描述回滚后事实。
- **步骤 3，complete A=[]（重放同一令牌）**：D={c,d}，边界 b→c 仍在，c、d 保
  资格；a 重挂待办，lease 清空。
- **步骤 4，complete A=[poison]（stale）**：t1 已在步骤 3 接纳删除，重复完成仅
  一次 lease 查找即 stale，无图维护读取，状态不变（checks.py:34）。

### 7.4 growth 的局部读取计划（locality，方案预测而非 trace 伪造）

两种 size 下变化都只是 a 删 a→c：D 恒为 {c,d}，边界恒为 [b→c]，读取计划完全
相同——page 点读 [a,b,c,d]（u、D 两点、边界来源 b），out 查询 [a,c,d]
（u 的换边前邻接一次，新出链来自 complete 入参不查库；D 内 c、d 各展开一次），
in 查询 [c,d]。size=1000 时 u0…u999 整条链在 D 外、与边界无涉，不产生任何维护
读取；新增成本只剩 edge 表上 s→u0 等边在**初始化建库**时的一次性 B 树写入，以及
运行期令牌查找 O(log V) 的索引深度微增。
这解释了“增加无关链为何不增加维护工作”：访问上界只由 d、e_D、b 决定，与 V、E
无关（最坏 D=全图时才退化到全图代价）。

## 8. 可重跑观察与核验接通

- `python3 -B evidence.py baseline`：现有全部回归（含 oracle 随机小图与增长
  断言），输出 BASELINE_OK，代表全图基线实测；
- `python3 -B evidence.py observe`：输出文末 actual 块的完整录制，含逐步
  op/result/state/reads 与 growth 计数；
- `python3 -B evidence.py verify`：先跑 baseline，再由 verify_document
  （evidence.py:26-68）核验标记唯一性、JSON 块可解析、actual 与实时 observe 逐字节
  相等、predicted 的步骤覆盖与状态一致、locality 覆盖 size=0/1000、源码引用的文件
  与行号有效（evidence.py:62-67）。`.pointwise-runtime.json` 的 verify 已按约定切到
  该命令，demo 保留 observe 真实观察。
- 边界声明：本设计不涉及种子变更、网络、重定向、HTML 解析、跨进程、崩溃恢复、
  历史迁移、正文回收或优先级调度；局部维护器未实现，任何超出 actual 的数字均为
  基于第 3-6 节伪代码的预测，可在实现后用同一 trace 口径与 checks.py 的 oracle
  对拍验证。

<!-- crawl-evidence -->
~~~json
{
  "actual": {
    "cases": {
      "shared": [
        {
          "op": [
            "claim",
            "c",
            "C"
          ],
          "result": {
            "url": "c",
            "token": "t1"
          },
          "state": {
            "eligible": [
              "a",
              "b",
              "c",
              "d",
              "s"
            ],
            "pending": [
              "a",
              "b",
              "d",
              "s"
            ],
            "active": [
              {
                "url": "c",
                "token": "t1",
                "generation": 1
              }
            ],
            "links": [
              [
                "a",
                "c"
              ],
              [
                "b",
                "c"
              ],
              [
                "c",
                "d"
              ],
              [
                "d",
                "c"
              ],
              [
                "s",
                "a"
              ],
              [
                "s",
                "b"
              ]
            ]
          },
          "reads": []
        },
        {
          "op": [
            "claim",
            "a",
            "A"
          ],
          "result": {
            "url": "a",
            "token": "t2"
          },
          "state": {
            "eligible": [
              "a",
              "b",
              "c",
              "d",
              "s"
            ],
            "pending": [
              "b",
              "d",
              "s"
            ],
            "active": [
              {
                "url": "a",
                "token": "t2",
                "generation": 1
              },
              {
                "url": "c",
                "token": "t1",
                "generation": 1
              }
            ],
            "links": [
              [
                "a",
                "c"
              ],
              [
                "b",
                "c"
              ],
              [
                "c",
                "d"
              ],
              [
                "d",
                "c"
              ],
              [
                "s",
                "a"
              ],
              [
                "s",
                "b"
              ]
            ]
          },
          "reads": []
        },
        {
          "op": [
            "complete",
            "A",
            []
          ],
          "result": "accepted",
          "state": {
            "eligible": [
              "a",
              "b",
              "c",
              "d",
              "s"
            ],
            "pending": [
              "a",
              "b",
              "d",
              "s"
            ],
            "active": [
              {
                "url": "c",
                "token": "t1",
                "generation": 1
              }
            ],
            "links": [
              [
                "b",
                "c"
              ],
              [
                "c",
                "d"
              ],
              [
                "d",
                "c"
              ],
              [
                "s",
                "a"
              ],
              [
                "s",
                "b"
              ]
            ]
          },
          "reads": [
            [
              "lease",
              "t2"
            ],
            [
              "page",
              "a"
            ],
            [
              "all_pages",
              5
            ],
            [
              "all_edges",
              5
            ],
            [
              "page",
              "a"
            ],
            [
              "page",
              "b"
            ],
            [
              "page",
              "c"
            ],
            [
              "page",
              "d"
            ],
            [
              "page",
              "s"
            ]
          ]
        },
        {
          "op": [
            "claim",
            "b",
            "B"
          ],
          "result": {
            "url": "b",
            "token": "t3"
          },
          "state": {
            "eligible": [
              "a",
              "b",
              "c",
              "d",
              "s"
            ],
            "pending": [
              "a",
              "d",
              "s"
            ],
            "active": [
              {
                "url": "b",
                "token": "t3",
                "generation": 1
              },
              {
                "url": "c",
                "token": "t1",
                "generation": 1
              }
            ],
            "links": [
              [
                "b",
                "c"
              ],
              [
                "c",
                "d"
              ],
              [
                "d",
                "c"
              ],
              [
                "s",
                "a"
              ],
              [
                "s",
                "b"
              ]
            ]
          },
          "reads": []
        },
        {
          "op": [
            "complete",
            "B",
            []
          ],
          "result": "accepted",
          "state": {
            "eligible": [
              "a",
              "b",
              "s"
            ],
            "pending": [
              "a",
              "b",
              "s"
            ],
            "active": [],
            "links": [
              [
                "c",
                "d"
              ],
              [
                "d",
                "c"
              ],
              [
                "s",
                "a"
              ],
              [
                "s",
                "b"
              ]
            ]
          },
          "reads": [
            [
              "lease",
              "t3"
            ],
            [
              "page",
              "b"
            ],
            [
              "all_pages",
              5
            ],
            [
              "all_edges",
              4
            ],
            [
              "page",
              "a"
            ],
            [
              "page",
              "b"
            ],
            [
              "page",
              "c"
            ],
            [
              "page",
              "d"
            ],
            [
              "page",
              "s"
            ]
          ]
        }
      ],
      "epoch": [
        {
          "op": [
            "claim",
            "c",
            "OLD"
          ],
          "result": {
            "url": "c",
            "token": "t1"
          },
          "state": {
            "eligible": [
              "a",
              "c",
              "d",
              "s"
            ],
            "pending": [
              "a",
              "d",
              "s"
            ],
            "active": [
              {
                "url": "c",
                "token": "t1",
                "generation": 1
              }
            ],
            "links": [
              [
                "a",
                "c"
              ],
              [
                "c",
                "d"
              ],
              [
                "d",
                "c"
              ],
              [
                "s",
                "a"
              ]
            ]
          },
          "reads": []
        },
        {
          "op": [
            "claim",
            "s",
            "CUT"
          ],
          "result": {
            "url": "s",
            "token": "t2"
          },
          "state": {
            "eligible": [
              "a",
              "c",
              "d",
              "s"
            ],
            "pending": [
              "a",
              "d"
            ],
            "active": [
              {
                "url": "c",
                "token": "t1",
                "generation": 1
              },
              {
                "url": "s",
                "token": "t2",
                "generation": 1
              }
            ],
            "links": [
              [
                "a",
                "c"
              ],
              [
                "c",
                "d"
              ],
              [
                "d",
                "c"
              ],
              [
                "s",
                "a"
              ]
            ]
          },
          "reads": []
        },
        {
          "op": [
            "complete",
            "CUT",
            []
          ],
          "result": "accepted",
          "state": {
            "eligible": [
              "s"
            ],
            "pending": [
              "s"
            ],
            "active": [],
            "links": [
              [
                "a",
                "c"
              ],
              [
                "c",
                "d"
              ],
              [
                "d",
                "c"
              ]
            ]
          },
          "reads": [
            [
              "lease",
              "t2"
            ],
            [
              "page",
              "s"
            ],
            [
              "all_pages",
              4
            ],
            [
              "all_edges",
              3
            ],
            [
              "page",
              "a"
            ],
            [
              "page",
              "c"
            ],
            [
              "page",
              "d"
            ],
            [
              "page",
              "s"
            ]
          ]
        },
        {
          "op": [
            "claim",
            "s",
            "OPEN"
          ],
          "result": {
            "url": "s",
            "token": "t3"
          },
          "state": {
            "eligible": [
              "s"
            ],
            "pending": [],
            "active": [
              {
                "url": "s",
                "token": "t3",
                "generation": 1
              }
            ],
            "links": [
              [
                "a",
                "c"
              ],
              [
                "c",
                "d"
              ],
              [
                "d",
                "c"
              ]
            ]
          },
          "reads": []
        },
        {
          "op": [
            "complete",
            "OPEN",
            [
              "a"
            ]
          ],
          "result": "accepted",
          "state": {
            "eligible": [
              "a",
              "c",
              "d",
              "s"
            ],
            "pending": [
              "a",
              "c",
              "d",
              "s"
            ],
            "active": [],
            "links": [
              [
                "a",
                "c"
              ],
              [
                "c",
                "d"
              ],
              [
                "d",
                "c"
              ],
              [
                "s",
                "a"
              ]
            ]
          },
          "reads": [
            [
              "lease",
              "t3"
            ],
            [
              "page",
              "s"
            ],
            [
              "all_pages",
              4
            ],
            [
              "all_edges",
              4
            ],
            [
              "page",
              "a"
            ],
            [
              "page",
              "c"
            ],
            [
              "page",
              "d"
            ],
            [
              "page",
              "s"
            ]
          ]
        },
        {
          "op": [
            "complete",
            "OLD",
            [
              "poison"
            ]
          ],
          "result": "stale",
          "state": {
            "eligible": [
              "a",
              "c",
              "d",
              "s"
            ],
            "pending": [
              "a",
              "c",
              "d",
              "s"
            ],
            "active": [],
            "links": [
              [
                "a",
                "c"
              ],
              [
                "c",
                "d"
              ],
              [
                "d",
                "c"
              ],
              [
                "s",
                "a"
              ]
            ]
          },
          "reads": [
            [
              "lease",
              "t1"
            ]
          ]
        },
        {
          "op": [
            "claim",
            "c",
            "NEW"
          ],
          "result": {
            "url": "c",
            "token": "t4"
          },
          "state": {
            "eligible": [
              "a",
              "c",
              "d",
              "s"
            ],
            "pending": [
              "a",
              "d",
              "s"
            ],
            "active": [
              {
                "url": "c",
                "token": "t4",
                "generation": 3
              }
            ],
            "links": [
              [
                "a",
                "c"
              ],
              [
                "c",
                "d"
              ],
              [
                "d",
                "c"
              ],
              [
                "s",
                "a"
              ]
            ]
          },
          "reads": []
        },
        {
          "op": [
            "complete",
            "NEW",
            []
          ],
          "result": "accepted",
          "state": {
            "eligible": [
              "a",
              "c",
              "s"
            ],
            "pending": [
              "a",
              "c",
              "s"
            ],
            "active": [],
            "links": [
              [
                "a",
                "c"
              ],
              [
                "d",
                "c"
              ],
              [
                "s",
                "a"
              ]
            ]
          },
          "reads": [
            [
              "lease",
              "t4"
            ],
            [
              "page",
              "c"
            ],
            [
              "all_pages",
              4
            ],
            [
              "all_edges",
              3
            ],
            [
              "page",
              "a"
            ],
            [
              "page",
              "c"
            ],
            [
              "page",
              "d"
            ],
            [
              "page",
              "s"
            ]
          ]
        }
      ],
      "rollback": [
        {
          "op": [
            "claim",
            "a",
            "A"
          ],
          "result": {
            "url": "a",
            "token": "t1"
          },
          "state": {
            "eligible": [
              "a",
              "b",
              "c",
              "d",
              "s"
            ],
            "pending": [
              "b",
              "c",
              "d",
              "s"
            ],
            "active": [
              {
                "url": "a",
                "token": "t1",
                "generation": 1
              }
            ],
            "links": [
              [
                "a",
                "c"
              ],
              [
                "b",
                "c"
              ],
              [
                "c",
                "d"
              ],
              [
                "d",
                "c"
              ],
              [
                "s",
                "a"
              ],
              [
                "s",
                "b"
              ]
            ]
          },
          "reads": []
        },
        {
          "op": [
            "rollback",
            "A",
            [
              "new"
            ]
          ],
          "result": {
            "attempt": "accepted",
            "inside": {
              "eligible": [
                "a",
                "b",
                "c",
                "d",
                "new",
                "s"
              ],
              "pending": [
                "a",
                "b",
                "c",
                "d",
                "new",
                "s"
              ],
              "active": [],
              "links": [
                [
                  "a",
                  "new"
                ],
                [
                  "b",
                  "c"
                ],
                [
                  "c",
                  "d"
                ],
                [
                  "d",
                  "c"
                ],
                [
                  "s",
                  "a"
                ],
                [
                  "s",
                  "b"
                ]
              ]
            },
            "result": "rolled_back"
          },
          "state": {
            "eligible": [
              "a",
              "b",
              "c",
              "d",
              "s"
            ],
            "pending": [
              "b",
              "c",
              "d",
              "s"
            ],
            "active": [
              {
                "url": "a",
                "token": "t1",
                "generation": 1
              }
            ],
            "links": [
              [
                "a",
                "c"
              ],
              [
                "b",
                "c"
              ],
              [
                "c",
                "d"
              ],
              [
                "d",
                "c"
              ],
              [
                "s",
                "a"
              ],
              [
                "s",
                "b"
              ]
            ]
          },
          "reads": [
            [
              "lease",
              "t1"
            ],
            [
              "page",
              "a"
            ],
            [
              "all_pages",
              6
            ],
            [
              "all_edges",
              6
            ],
            [
              "page",
              "a"
            ],
            [
              "page",
              "b"
            ],
            [
              "page",
              "c"
            ],
            [
              "page",
              "d"
            ],
            [
              "page",
              "new"
            ],
            [
              "page",
              "s"
            ]
          ]
        },
        {
          "op": [
            "complete",
            "A",
            []
          ],
          "result": "accepted",
          "state": {
            "eligible": [
              "a",
              "b",
              "c",
              "d",
              "s"
            ],
            "pending": [
              "a",
              "b",
              "c",
              "d",
              "s"
            ],
            "active": [],
            "links": [
              [
                "b",
                "c"
              ],
              [
                "c",
                "d"
              ],
              [
                "d",
                "c"
              ],
              [
                "s",
                "a"
              ],
              [
                "s",
                "b"
              ]
            ]
          },
          "reads": [
            [
              "lease",
              "t1"
            ],
            [
              "page",
              "a"
            ],
            [
              "all_pages",
              5
            ],
            [
              "all_edges",
              5
            ],
            [
              "page",
              "a"
            ],
            [
              "page",
              "b"
            ],
            [
              "page",
              "c"
            ],
            [
              "page",
              "d"
            ],
            [
              "page",
              "s"
            ]
          ]
        },
        {
          "op": [
            "complete",
            "A",
            [
              "poison"
            ]
          ],
          "result": "stale",
          "state": {
            "eligible": [
              "a",
              "b",
              "c",
              "d",
              "s"
            ],
            "pending": [
              "a",
              "b",
              "c",
              "d",
              "s"
            ],
            "active": [],
            "links": [
              [
                "b",
                "c"
              ],
              [
                "c",
                "d"
              ],
              [
                "d",
                "c"
              ],
              [
                "s",
                "a"
              ],
              [
                "s",
                "b"
              ]
            ]
          },
          "reads": [
            [
              "lease",
              "t1"
            ]
          ]
        }
      ]
    },
    "growth": [
      {
        "size": 0,
        "reads": {
          "all_pages": 5,
          "all_edges": 5,
          "page_queries": 6,
          "lease_queries": 1
        }
      },
      {
        "size": 1000,
        "reads": {
          "all_pages": 1005,
          "all_edges": 1005,
          "page_queries": 1006,
          "lease_queries": 1
        }
      }
    ]
  },
  "predicted": {
    "shared": [
      {
        "step": 3,
        "eligible": [
          "a",
          "b",
          "c",
          "d",
          "s"
        ],
        "pending": [
          "a",
          "b",
          "d",
          "s"
        ],
        "active": [
          {
            "url": "c",
            "token": "t1",
            "generation": 1
          }
        ],
        "region": [
          "c",
          "d"
        ],
        "boundary": [
          [
            "b",
            "c"
          ]
        ],
        "why": "删边 a→c 的目标 c 在并图中闭包到 c、d（c↔d）；G' 中唯一进入 D 的边界是 b→c，来源 b 合格，支撑经 c 传到 d，c、d 全部保持资格，c 的 t1 令牌不动，a 只重挂待办，不漏停也不误停。"
      },
      {
        "step": 5,
        "eligible": [
          "a",
          "b",
          "s"
        ],
        "pending": [
          "a",
          "b",
          "s"
        ],
        "active": [],
        "region": [
          "c",
          "d"
        ],
        "boundary": [],
        "why": "b→c 删除后 D 仍为 c、d，但 G' 中进入 D 的边界为空；c↔d 是无种子、无合格外部来源的孤立环，环内旧资格不能互相当支撑，两页同时失格、代次加一、待办清空且 t1 立即失效，互链旧边按契约保留。"
      }
    ],
    "epoch": [
      {
        "step": 3,
        "eligible": [
          "s"
        ],
        "pending": [
          "s"
        ],
        "active": [],
        "region": [
          "a",
          "c",
          "d"
        ],
        "boundary": [],
        "why": "s 删除唯一出边 s→a，目标 a 在并图中闭包到 a、c、d；G' 没有任何从 D 外进入 D 的边，三页失去全部外部支撑而失格，c 的旧令牌随翻转删除，D 外的 s 资格与待办不变。"
      },
      {
        "step": 5,
        "eligible": [
          "a",
          "c",
          "d",
          "s"
        ],
        "pending": [
          "a",
          "c",
          "d",
          "s"
        ],
        "active": [],
        "region": [
          "a",
          "c",
          "d"
        ],
        "boundary": [
          [
            "s",
            "a"
          ]
        ],
        "why": "新增 s→a，目标 a 闭包到 a、c、d；边界 s→a 的来源 s 合格，支撑沿 a→c→d 传播，三页以新代次恢复并进待办，旧代次令牌已不存在，无法回流。"
      },
      {
        "step": 6,
        "eligible": [
          "a",
          "c",
          "d",
          "s"
        ],
        "pending": [
          "a",
          "c",
          "d",
          "s"
        ],
        "active": [],
        "region": [],
        "boundary": [],
        "why": "旧令牌 t1 已在断链步骤删除，一次 lease 查找即返回 stale；不构造影响区、不读邻接、不写任何行，poison 完全忽略，状态与上一步逐字节相同。"
      },
      {
        "step": 8,
        "eligible": [
          "a",
          "c",
          "s"
        ],
        "pending": [
          "a",
          "c",
          "s"
        ],
        "active": [],
        "region": [
          "c",
          "d"
        ],
        "boundary": [
          [
            "a",
            "c"
          ]
        ],
        "why": "删 c→d，目标 d 借并图旧边 d→c、c→d 闭包出 D={c,d}；G' 边界 a→c 的来源 a 合格，支撑只到 c，c 已无出边故 d 得不到支撑而失格，c 保资格并重挂待办，D 外页面零访问。"
      }
    ],
    "rollback": [
      {
        "step": 2,
        "eligible": [
          "a",
          "b",
          "c",
          "d",
          "s"
        ],
        "pending": [
          "b",
          "c",
          "d",
          "s"
        ],
        "active": [
          {
            "url": "a",
            "token": "t1",
            "generation": 1
          }
        ],
        "region": [
          "c",
          "d",
          "new"
        ],
        "boundary": [
          [
            "a",
            "new"
          ],
          [
            "b",
            "c"
          ]
        ],
        "why": "尝试把 a 的出链由 c 换成 new：变化目标 c、new 在并图闭包为 c、d、new；G' 边界 a→new、b→c 的来源均合格，支撑覆盖 c、d、new，故事务内 new 取得资格；外层 ROLLBACK 撤销换边、新页与全部资格/待办/令牌变更，回滚后与领取后状态一致，t1 仍有效。region/boundary 描述被撤销的尝试。"
      },
      {
        "step": 3,
        "eligible": [
          "a",
          "b",
          "c",
          "d",
          "s"
        ],
        "pending": [
          "a",
          "b",
          "c",
          "d",
          "s"
        ],
        "active": [],
        "region": [
          "c",
          "d"
        ],
        "boundary": [
          [
            "b",
            "c"
          ]
        ],
        "why": "同一令牌重放空出链：D={c,d}，边界 b→c 仍在且 b 合格，c、d 保资格、无令牌失效；a 完成后重新进入待办。"
      },
      {
        "step": 4,
        "eligible": [
          "a",
          "b",
          "c",
          "d",
          "s"
        ],
        "pending": [
          "a",
          "b",
          "c",
          "d",
          "s"
        ],
        "active": [],
        "region": [],
        "boundary": [],
        "why": "t1 已在上一步接纳并删除，重复 complete 仅一次 lease 查找即 stale，无图维护读取、无写入，poison 不入图。"
      }
    ]
  },
  "locality": [
    {
      "size": 0,
      "region": [
        "c",
        "d"
      ],
      "boundary": [
        [
          "b",
          "c"
        ]
      ],
      "page_reads": [
        "a",
        "b",
        "c",
        "d"
      ],
      "out_reads": [
        "a",
        "c",
        "d"
      ],
      "in_reads": [
        "c",
        "d"
      ],
      "why": "变化仅为 a 删除 a→c：并图影响区恒为 {c,d}，G' 入边边界恒为 b→c。page 点读覆盖完成页 a、D 内 c/d 与边界来源 b；out 查询为 a 的换边前邻接一次（新出链直接取自 complete 入参，不查库）加 D 内 c、d 的邻接展开；in 查询对 c、d 各走一次 edge_by_dst。千页独立链 u0…u999 全在 D 外且不接触边界，维护读取与 size=0 完全相同；其成本只体现在初始化建库的边/索引 B 树写入，以及 lease 等查找 O(log V) 的索引深度，不进入维护 trace，故无关分支增长不增加维护工作。"
    },
    {
      "size": 1000,
      "region": [
        "c",
        "d"
      ],
      "boundary": [
        [
          "b",
          "c"
        ]
      ],
      "page_reads": [
        "a",
        "b",
        "c",
        "d"
      ],
      "out_reads": [
        "a",
        "c",
        "d"
      ],
      "in_reads": [
        "c",
        "d"
      ],
      "why": "变化仅为 a 删除 a→c：并图影响区恒为 {c,d}，G' 入边边界恒为 b→c。page 点读覆盖完成页 a、D 内 c/d 与边界来源 b；out 查询为 a 的换边前邻接一次（新出链直接取自 complete 入参，不查库）加 D 内 c、d 的邻接展开；in 查询对 c、d 各走一次 edge_by_dst。千页独立链 u0…u999 全在 D 外且不接触边界，维护读取与 size=0 完全相同；其成本只体现在初始化建库的边/索引 B 树写入，以及 lease 等查找 O(log V) 的索引深度，不进入维护 trace，故无关分支增长不增加维护工作。"
    }
  ]
}
~~~
