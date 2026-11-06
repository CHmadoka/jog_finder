# job_finder

通过**已登录浏览器的招聘网站账号**(Boss直聘 / 前程无忧 / 猎聘),自动按筛选条件抓取岗位详情,
经"HR 活跃度 / 最近发布"过滤与数据库查重后存入本地 SQLite,并自动在备注中写明"不符合原因"。

## 功能特性

- **接管真实浏览器**:通过 Chrome/Edge 的 CDP 调试端口连接,登录态天然保留、指纹真实、风控风险低
- **三平台独立抓取**:每个平台同时最多一个抓取任务,平台之间互不影响
- **"档位"式岗位筛选**:一个标签名(如"Java后端")+ 多个具体岗位词条(如 Java、Java开发、服务端)
- **活跃度过滤**:只入库 HR 最近活跃 / 岗位最近发布的岗位(阈值可配置,默认 7 天)
- **自动查重**:`(平台, 岗位ID)` 唯一键,已入库岗位不重复保存
- **自动备注**:关键词规则 + 可选 LLM 分析职位描述,把"岗位要求不符 / 加班 / 压力大 / 出差驻场"等原因写入备注
- **本地 Web 界面**:档位与条件管理、任务启停与实时进度、岗位浏览(分页/筛选/搜索)、详情查看、Excel 导出

## 环境要求

- Windows 10/11,Python 3.11+
- 本机安装 Chrome 或 Edge
- (可选)LLM API:OpenAI 兼容接口(用于自动备注;不配置则仅用关键词规则)

## 安装

```powershell
cd D:\git_repo\job_finder
python -m venv --copies .venv        # 若默认方式报 Permission denied,保留 --copies
.venv\Scripts\python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
Copy-Item .env.example .env          # 按需修改配置
```

无需安装 Playwright 自带浏览器(只用 CDP 接管系统 Chrome/Edge)。

## 配置说明(.env)

| 配置项 | 说明 |
|---|---|
| `BROWSER_PATH` | 浏览器 exe 路径;留空则自动探测 Chrome/Edge 常见位置 |
| `CDP_PORT` | 调试端口,默认 9222 |
| `BROWSER_PROFILE_DIR` | 专用浏览器 profile 目录(登录态保存在这里) |
| `HR_ACTIVE_DAYS` / `JOB_PUBLISH_DAYS` | 活跃度窗口天数,默认 7 |
| `DELAY_MIN/MAX`、`MAX_PAGES_PER_TASK` | 防风控节奏与单任务页数上限 |
| `LLM_API_BASE` / `LLM_API_KEY` / `LLM_MODEL` | OpenAI 兼容 LLM;留空 key 则禁用 |

> Chrome 136+ 禁止对默认用户目录开启 CDP,因此本工具使用**独立 profile**:
> 首次拉起的浏览器窗口中,请手动登录三个平台,登录态会持久保存,之后无需重复登录。

## 使用流程

**一键启动**:双击项目根目录的 **`启动.bat`** —— 自动检查服务是否在运行、按需初始化环境、
以最小化窗口启动服务并自动打开 `http://127.0.0.1:8765`(双击 **`停止.bat`** 即可停止)。

也可以手动启动:

```powershell
.venv\Scripts\python -m app.main
```

**抓取操作(左栏,无需预先建条件)**:

1. 点选平台卡片(Boss直聘 / 前程无忧 / 猎聘)
2. 选择**岗位档位**(一次任务自动跑该档位的**全部词条**,对齐多关键词批量采集)
3. 填城市(留空=全国)、每词条页数 → 点**开始抓取**
4. 左栏实时显示:当前词条进度、发现/入库/跳过计数、最近任务历史
5. 需要复用的配置点"存为条件";已存条件可在"筛选条件"里编辑(含避开/优先行业)

**结果浏览(右栏)**:岗位表格含行业列与"契合/不符"备注列;点击行看详情
(职位描述、HR活跃、技能标签);支持按匹配情况筛选、搜索、导出 Excel、"清空岗位库"。

> 2026-11 实测对齐 boss-search 实例:Boss 采用接口直连(`/wapi/zpgeek/search/joblist.json`,
> 明文薪资/行业/HR在线),薪资字体反爬(PUA 字形)canvas 自动解码,详情携带 securityId/lid
> 防重定向,下线岗位自动跳过。武汉"数据处理"单页实测:发现 30 → 入库 27,全部带描述与行业。

## 筛选条件说明

- **岗位档位**:一个标签名 + 多个岗位词条(如"Java后端":Java、Java开发、服务端)
- **筛选条件**:平台 + 城市 + 档位 + 关键词,并支持**行业偏好**:
  - **避开行业**(如金融、电商、游戏):命中后自动在备注写明"行业不符"
  - **优先行业**(如制造业、医疗、学校):命中后备注标记"行业契合"
- 条件可在界面上随时**编辑/删除**,不影响已入库数据
- **备注格式**:`契合: 行业契合: 医疗服务 | 不符: [压力] 工作强度大;标题未直接命中档位[xx]词条`

> Boss 直聘抓取采用**接口直连**(页面内 `/wapi/zpgeek/search/joblist.json`,带登录态),
> 薪资字体反爬(PUA 字形)通过 canvas 模板匹配自动解码;详情 URL 携带 securityId/lid
> 防止被重定向。方案来自 2026-11 实测验证。

## 架构

```
Web界面(Vue3+ElementPlus) ──FastAPI──┐
                                     ├─ 调度器(每平台1并发) ─ 平台适配器(job51/boss/liepin)
SQLite(job_finder.db) ←──── 过滤引擎(活跃度/查重/规则+LLM备注)
                                     │
                              Playwright CDP ── 接管用户 Chrome/Edge
```

- `app/platforms/`:平台适配器。优先解析页面内嵌 JSON 与 JSON-LD,DOM 选择器兜底;
  所有选择器集中在 `selectors.py`,失效时先改这里
- `app/scheduler/`:任务调度与查重入库
- `app/filter/`:活跃度窗口、关键词规则、LLM 结构化分析
- 解析失败会自动把页面快照存到 `data/snapshots/`,便于修复选择器

## 运行测试

```powershell
.venv\Scripts\python -m unittest discover -s tests
```

## 常见问题

- **任务报"未登录"**:在自动拉起的浏览器窗口里登录对应平台,然后重新启动任务
- **Boss直聘提示"安全验证"**:程序会等待最多 120 秒,请在浏览器里手动完成滑块验证
- **列表解析不到岗位(0 条)**:平台改版导致选择器失效,查看 `data/snapshots/` 中的快照,
  调整 `app/platforms/selectors.py` 后重启服务
- **端口被占用**:修改 `.env` 中的 `PORT`(Web)或 `CDP_PORT`(浏览器调试端口)
