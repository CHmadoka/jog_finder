/* job_finder 前端逻辑(Vue3 全局构建 + Element Plus)
 *
 * 设计原则:
 * 1. 抓取(左栏)与数据浏览(右栏)完全解耦:左栏平台只决定"往哪抓",右栏筛选只决定"看什么"。
 * 2. 快速抓取 / 已存条件双模式,所选条件实时展示摘要,避免"黑盒启动"。
 * 3. 启动前置校验:条件不完整时按钮禁用并给出明确原因。
 */
const { createApp, reactive, ref, computed, onMounted, onUnmounted, watch } = Vue;
const { ElMessage, ElMessageBox } = ElementPlus;

const PLATFORM_NAMES = { boss: "Boss直聘", job51: "前程无忧", liepin: "猎聘" };
// 平台徽标(左栏平台按钮):三家官方应用图标(1024px 无损源图)已放进 web/
// (经 /assets 静态映射,离线可用);图片缺失/加载失败时回退到品牌色文字徽标。
// 1x/2x/3x 三档文件由 scripts/gen_logos.py 按 .plogo 的 CSS 尺寸生成,
// 浏览器按屏幕像素比直接挑像素对齐的那张,不做运行时缩放(不会发虚)
function logoSrcset(base) {
  return [`${base}.png 1x`, `${base}@2x.png 2x`, `${base}@3x.png 3x`].join(", ");
}
const PLATFORM_LOGOS = {
  boss: { img: "/assets/logo_boss.png", srcset: logoSrcset("/assets/logo_boss"), text: "B", color: "#00a6a7" },
  job51: { img: "/assets/logo_51job.png", srcset: logoSrcset("/assets/logo_51job"), text: "51", color: "#ff6000" },
  liepin: { img: "/assets/logo_liepin.png", srcset: logoSrcset("/assets/logo_liepin"), text: "猎", color: "#d8262c" },
};
const SearchIcon = ElementPlusIconsVue.Search;
const RefreshIcon = ElementPlusIconsVue.Refresh;
const LinkIcon = ElementPlusIconsVue.Link;
const DeleteIcon = ElementPlusIconsVue.Delete;
const SettingIcon = ElementPlusIconsVue.Setting;
const TABLE_MEM_KEY = "jf_current_table";   // 本地记忆:右栏浏览的数据表
const GRAB_TABLE_KEY = "jf_grab_table";     // 本地记忆:左栏存入目标表(与浏览独立)
const VIEW_PLATFORM_KEY = "jf_view_platform"; // 本地记忆:右栏浏览的平台筛选
const VIEW_TASK_KEY = "jf_view_task";       // 本地记忆:右栏的任务过滤

// 词条/行业标签输入器:回车添加、必清空、可删除(替代 el-select allow-create,
// 规避其创建后输入文本残留的问题)
const KwEditor = {
  props: {
    modelValue: { type: Array, default: () => [] },
    placeholder: { type: String, default: "输入后回车添加" },
  },
  emits: ["update:modelValue"],
  setup(props, { emit }) {
    const draft = Vue.ref("");
    function add() {
      const v = draft.value.trim();
      draft.value = "";                       // 无论是否重复都清空输入
      if (!v) return;
      const next = props.modelValue.includes(v) ? props.modelValue : [...props.modelValue, v];
      emit("update:modelValue", next);
    }
    function remove(i) {
      emit("update:modelValue", props.modelValue.filter((_, idx) => idx !== i));
    }
    return { draft, add, remove };
  },
  template: `
    <div class="kw-editor">
      <div class="kw-tags" v-show="modelValue.length">
        <el-tag v-for="(k, i) in modelValue" :key="k" size="small" effect="plain" closable
                @close="remove(i)">{{ k }}</el-tag>
      </div>
      <el-input v-model="draft" size="small" :placeholder="placeholder" @keyup.enter="add"></el-input>
    </div>`,
};

// 城市选择器:省→市两级级联选择(按行政级别分级),选中即加为标签(可多选)。
// 与 KwEditor 同容器风格:标签展示已选 + 选择器追加;叶子 value 为城市短名,
// 与平台 city_codes 键一致(数据源 web/cities.js,生成脚本 scripts/gen_cities.py)
const CityPicker = {
  props: {
    modelValue: { type: Array, default: () => [] },
    placeholder: { type: String, default: "选择省 → 市后添加" },
  },
  emits: ["update:modelValue"],
  setup(props, { emit }) {
    const options = window.CN_PROVINCE_CITIES || [];
    const picked = Vue.ref([]);
    function onChange(vals) {
      const v = Array.isArray(vals) ? vals[vals.length - 1] : "";
      if (v && !props.modelValue.includes(v)) {
        emit("update:modelValue", [...props.modelValue, v]);
      }
      picked.value = [];   // 添加后即清空选择器,便于继续选下一个城市
    }
    function remove(i) {
      emit("update:modelValue", props.modelValue.filter((_, idx) => idx !== i));
    }
    return { options, picked, onChange, remove };
  },
  template: `
    <div class="kw-editor">
      <div class="kw-tags" v-show="modelValue.length">
        <el-tag v-for="(c, i) in modelValue" :key="c + i" size="small" effect="plain" closable
                @close="remove(i)">{{ c }}</el-tag>
      </div>
      <el-cascader v-model="picked" :options="options" :props="{ expandTrigger: 'hover' }"
                   size="small" :placeholder="placeholder" :show-all-levels="false" clearable
                   @change="onChange"></el-cascader>
    </div>`,
};

const app = createApp({
  setup() {
    // ---------- 状态 ----------
    const platforms = ref([]);
    const gears = ref([]);
    const filters = ref([]);
    const tables = ref([]);
    const jobs = reactive({ total: 0, items: [] });
    const stats = reactive({ total: 0, fit: 0, unfit: 0, online: 0 });
    const recentTasks = ref([]);
    // 最近任务卡片滚动加载状态:limit 递增(5→15→25…),done=返回条数<limit
    const taskList = reactive({ limit: 5, loading: false, done: false });
    const taskOptions = ref([]);      // 左栏任务选择框候选(当前平台最近30条)
    const taskMgrVisible = ref(false);
    const healthText = ref("检测连接...");
    const loading = ref(false);
    const starting = ref(false);

    // 左栏:抓取配置(只影响"发起抓取")
    // table_id: 存入目标表(与右栏浏览筛选完全分离,新岗位固定存入该表)
    const grab = reactive({
      platform: "boss",
      mode: "quick",            // quick | saved
      filter_id: null,
      gear_id: null,
      cities: [],               // 多选城市(省市级联选择;空 = 全国)
      salary_min: "",           // 薪资范围下限(K/月,可缺省)
      salary_max: "",           // 薪资范围上限(K/月,可缺省)
      max_pages: 10,
      table_id: 1,              // 抓取结果存入的数据表
    });

    // 右栏:岗位浏览筛选(默认按最后更新时间倒序)
    // 数据表=当前工作表:既决定看哪个表的岗位,也是新抓岗位的存入目标
    // task_id: 按抓取任务过滤(任务详情"查看关联岗位"/岗位详情"来源任务"入口)
    const query = reactive({
      platform: "", table_id: 1, fit: "", q: "", task_id: null,
      sort_by: "updated", sort_order: "desc", page: 1, page_size: 20,
    });

    const statuses = reactive({});   // platform -> { running, progress, counters }
    const currentJob = ref(null);
    const showDetail = ref(false);
    const gearVisible = ref(false);
    const filterVisible = ref(false);
    const tableVisible = ref(false);
    const taskDetailVisible = ref(false);
    const detailTask = ref(null);
    const events = ref([]);
    const eventsOpen = ref(false);
    const gearForm = reactive({ id: null, name: "", keywords: [] });
    const filterForm = reactive({
      id: null, platform: "boss", name: "", cities: [], gear_id: null,
      salary_min: "", salary_max: "",
      industries_avoid: [], industries_prefer: [],
    });
    const tableForm = reactive({ id: null, name: "", slug: "" });
    let timer = null;

    // ---------- 计算属性 ----------
    const platformName = computed(
      () => platformOf(grab.platform)
    );
    const selectedFilter = computed(
      () => filters.value.find((f) => f.id === grab.filter_id) || null
    );
    const currentStatus = computed(() => statuses[grab.platform] || {});
    const running = computed(() => !!currentStatus.value.running);

    const canStart = computed(() => {
      if (grab.mode === "saved") return !!grab.filter_id;
      return !!grab.gear_id;
    });
    const startHint = computed(() => {
      if (canStart.value) return "";
      if (grab.mode === "saved") return "请先选择一个已存条件";
      return "请先选择岗位设置";
    });
    // 表头排序指示器的受控状态(与 query.sort_by/sort_order 同步)
    const SORT_PROP = { updated: "updated_at", published: "published_at", hr_active: "hr_active_at", city: "city", exp_edu: "experience" };
    const sortState = computed(() => [
      { prop: SORT_PROP[query.sort_by] || "updated_at", order: query.sort_order === "asc" ? "ascending" : "descending" },
    ]);
    // 跳过原因明细(运行中/结束时展示)
    const skipTip = computed(() => {
      const s = (currentStatus.value.counters || {}).skip_stats || {};
      const txt = Object.entries(s).map(([k, v]) => `${k} ${v}`).join(",");
      return txt || "";
    });
    // 存入目标表名(左栏抓取配置;与右栏浏览的工作表选择无关)
    const currentTableName = computed(() => {
      const t = tables.value.find((x) => x.id === (grab.table_id || 1));
      return t ? t.name : "默认表";
    });

    // 任务组合进度:默认只显示当前组合行,悬停向下展开全部组合(词条×城市)
    const comboHover = ref(false);
    const comboCur = computed(() => {
      const cb = currentStatus.value.combos;
      if (!cb || !cb.items) return null;
      const idx = cb.items.findIndex((c) => c.state === "running");
      if (idx < 0) return null;
      return { idx: idx + 1, kw: cb.items[idx].kw, city: cb.items[idx].city };
    });

    // ---------- 工具 ----------
    async function api(path, opts = {}) {
      const resp = await fetch("/api" + path, {
        headers: { "Content-Type": "application/json" },
        ...opts,
        body: opts.body ? JSON.stringify(opts.body) : undefined,
      });
      if (!resp.ok) {
        let msg = `HTTP ${resp.status}`;
        try { msg = (await resp.json()).detail || msg; } catch (e) {}
        ElMessage.error(typeof msg === "string" ? msg : JSON.stringify(msg));
        throw new Error(msg);
      }
      const ct = resp.headers.get("content-type") || "";
      return ct.includes("json") ? resp.json() : resp;
    }
    function platformOf(pid) {
      return (platforms.value.find((p) => p.id === pid) || {}).name
        || PLATFORM_NAMES[pid] || pid;
    }
    const logoBroken = reactive({});   // 徽标图片加载失败过的平台
    function platformLogo(pid) {
      const cfg = PLATFORM_LOGOS[pid];
      if (!cfg) return { img: "", srcset: "", text: (pid || "?").slice(0, 1).toUpperCase(), color: "#8a94a6" };
      // 图片 404/解码失败时降级为文字徽标,避免出现裂图占位
      if (logoBroken[pid]) return { img: "", srcset: "", text: cfg.text, color: cfg.color };
      return cfg;
    }
    function onLogoError(pid) {
      logoBroken[pid] = true;
    }
    function gearName(gid) {
      return (gears.value.find((g) => g.id === gid) || {}).name || "设置#" + gid;
    }
    function isRunning(pid) {
      const s = statuses[pid];
      if (s && s.running !== undefined) return !!s.running;
      const p = platforms.value.find((x) => x.id === pid);
      return !!(p && p.running);
    }
    // 平台按钮的"最近一次任务结果"临时状态:running 优先;最近任务 error→红灯、
    // done→绿边框;手动停止(stopped)为中性不变色。新任务启动即覆盖(run 优先)
    function taskStatusOf(pid) {
      const s = statuses[pid];
      if (s && s.running) return "running";
      const last = s && s.last;
      if (!last) return "";
      if (last.status === "error") return "error";
      if (last.status === "done") return "done";
      return "";
    }
    function fmtTime(s) { return s ? String(s).replace("T", " ").slice(0, 16) : "-"; }
    function fmtDate(s) { return s ? String(s).slice(5, 10) : "-"; }
    function fmtSalaryRange(raw) {
      // "10,20" → "10-20K";"10," → "≥10K";",20" → "≤20K";空 → 不限
      const s = (raw || "").trim();
      if (!s) return "不限";
      const [lo, hi] = s.split(",");
      if (lo && hi) return `${lo}-${hi}K`;
      if (lo) return `≥${lo}K`;
      if (hi) return `≤${hi}K`;
      return "不限";
    }
    function composeSalary(min, max) {
      // 双输入 → "min,max"(单边可缺省);均为空返回 null 表示不限
      const lo = (min || "").trim(), hi = (max || "").trim();
      if (!lo && !hi) return null;
      const numRe = /^\d+(\.\d+)?$/;
      if ((lo && !numRe.test(lo)) || (hi && !numRe.test(hi))) return "INVALID";
      return `${lo},${hi}`;
    }
    // 逗号分隔多城市串 → 顿号展示(空 = 全国);任务记录/条件均以此格式存储
    function fmtCities(s) {
      const arr = (s || "").split(",").map((x) => x.trim()).filter(Boolean);
      return arr.length ? arr.join("、") : "全国";
    }
    function activeTagType(label) {
      if (/今日|昨日|刚刚/.test(label)) return "success";
      if (label.includes("天内")) return "primary";
      return "info";                       // N天前活跃 → 中性灰
    }
    function activeTip(row) {
      const t = row.hr_active_text;
      if (t && t !== row.hr_active_label) return `平台原文: ${t} · 估算基准: ${row.hr_active_at || "-"}`;
      return `估算基准: ${row.hr_active_at || "-"}`;
    }
    function taskTagType(s) {
      return { done: "success", running: "primary", stopped: "warning", error: "danger" }[s] || "info";
    }
    // 结束任务行的结构化标签数据:m词条 / n城市(空=全国) / a/b入库
    function kwCount(t) {
      return (t.keywords || "").split("|").filter((x) => x.trim()).length;
    }
    function cityCount(t) {
      return (t.city || "").split(",").map((x) => x.trim()).filter(Boolean).length;
    }
    function cityLabel(t) {
      const n = cityCount(t);
      return n ? n + "城市" : "全国";
    }
    function taskText(s) {
      return { done: "完成", running: "进行中", stopped: "已停止", error: "失败" }[s] || s;
    }
    // 任务详情卡片
    function showTaskDetail(t) {
      detailTask.value = t;
      taskDetailVisible.value = true;
    }
    // 详情抽屉福利标签:兼容顿号/逗号/分号多种分隔,并去掉空片段
    const welfareTags = computed(() => {
      const w = (currentJob.value || {}).welfare || "";
      return w.split(/[,，;；、]+/).map((s) => s.trim()).filter(Boolean);
    });

    // ---------- 词条高亮(浏览岗位详情) ----------
    // 词条来源 = 岗位记录的 keyword(该任务全部搜索词条,顿号/竖线分隔)。
    // 展示时动态扫描标题/描述文本命中情况——不依赖入库时新增字段,旧数据同样生效。
    function escapeHtml(s) {
      return String(s).replace(/[&<>"']/g, (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    }
    function escapeReg(s) {
      return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    }
    const jobKeywords = computed(() => {
      const raw = (currentJob.value || {}).keyword || "";
      return [...new Set(raw.split(/[、,，|;；]+/).map((s) => s.trim()).filter(Boolean))];
    });
    // 命中词条分两态:
    // - 精确命中(exact=true,黄色):词条出现在标题/描述文本中;
    // - 关联/扩展(exact=false,蓝色+关联图标):来源词条(source_keyword,
    //   该岗位由哪个词条的搜索结果中抓出)但文本未精确出现——平台按词条
    //   扩展返回的相关岗位(如 数据开发↔数据处理),颜色与图标区分。
    // 大小写不敏感——与入库匹配规则一致(evaluate_job 用 lower() 比较)。
    const hitKeywords = computed(() => {
      const j = currentJob.value || {};
      const hay = `${(j.title || "").toLowerCase()}\n${(j.description || "").toLowerCase()}`;
      const hit = (k) => hay.includes(k.toLowerCase());
      const src = (j.source_keyword || "").trim();
      const out = [];
      const seen = new Set();
      if (src) {
        const exact = hit(src);
        out.push({
          kw: src, exact,
          tip: exact
            ? "来源词条:出现在标题或描述中(精确匹配)"
            : "关联词条:本岗位由该词条的搜索结果扩展抓取(文本未精确出现)",
        });
        seen.add(src);
      }
      for (const k of jobKeywords.value) {
        if (seen.has(k) || !hit(k)) continue;
        out.push({ kw: k, exact: true, tip: "出现在本岗位标题或描述中(精确匹配)" });
        seen.add(k);
      }
      return out;
    });
    // 通用高亮:文本先 HTML 转义(防 XSS),再按词条(长词优先,避免
    // "文员"截胡"仓库文员")大小写不敏感地替换为 <mark>
    function highlightText(text) {
      const s = String(text || "");
      if (!s) return "";
      const esc = escapeHtml(s);
      const lower = s.toLowerCase();
      const parts = jobKeywords.value
        .filter((k) => lower.includes(k.toLowerCase()))
        .sort((a, b) => b.length - a.length)
        .map((k) => escapeReg(escapeHtml(k)));
      if (!parts.length) return esc;
      return esc.replace(new RegExp(`(${parts.join("|")})`, "gi"), "<mark>$1</mark>");
    }
    const highlightedDesc = computed(() => highlightText((currentJob.value || {}).description));
    // 标题高亮:入库匹配规则同样看标题,标题一并高亮
    const highlightedTitle = computed(() => highlightText((currentJob.value || {}).title));
    // 异常面板为悬浮二级卡片:点击面板与唤起按钮以外的空白处即收起
    function onDocClick(ev) {
      if (!eventsOpen.value) return;
      const t = ev.target;
      if (t.closest && (t.closest(".event-panel") || t.closest(".event-fab"))) return;
      eventsOpen.value = false;
    }

    // ---------- 数据加载 ----------
    async function loadPlatforms() {
      platforms.value = await api("/platforms");
      if (!platforms.value.some((p) => p.id === grab.platform) && platforms.value.length) {
        grab.platform = platforms.value[0].id;
      }
    }
    async function loadGears() { gears.value = await api("/gears"); }
    async function loadTables() { tables.value = await api("/tables"); }
    async function loadFilters() {
      if (!grab.platform) return;
      filters.value = await api(`/filters?platform=${grab.platform}`);
    }
    async function loadJobs() {
      loading.value = true;
      try {
        const params = new URLSearchParams();
        if (query.platform) params.set("platform", query.platform);
        if (query.task_id) params.set("task_id", query.task_id);
        else params.set("table_id", query.table_id || 1);   // 任务过滤时不按工作表截断
        if (query.fit) params.set("fit", query.fit);
        if (query.q.trim()) params.set("q", query.q.trim());
        params.set("page", query.page);
        params.set("page_size", query.page_size);
        params.set("sort_by", query.sort_by);
        params.set("sort_order", query.sort_order);
        const data = await api("/jobs?" + params.toString());
        jobs.total = data.total;
        jobs.items = data.items;
      } finally {
        loading.value = false;
      }
    }
    async function loadStats() {
      const params = new URLSearchParams();
      if (query.platform) params.set("platform", query.platform);
      if (query.task_id) params.set("task_id", query.task_id);
      else params.set("table_id", query.table_id || 1);
      if (query.q.trim()) params.set("q", query.q.trim());
      Object.assign(stats, await api("/jobs/stats?" + params.toString()));
    }
    async function loadEvents() {
      try { events.value = await api("/events?limit=50"); } catch (e) {}
    }
    async function clearEvents() {
      const r = await api("/events", { method: "DELETE" });
      ElMessage.success(`已清除 ${r.deleted} 条异常记录`);
      loadEvents();
    }
    async function loadTasks() {
      // 最近任务 = 全部平台混排(带平台徽标):任务记录不应被左栏当前平台隐藏,
      // 否则"刚跑完的任务在界面上找不到"——用户在 boss 视图下看不到 job51/liepin 的记录
      // 卡片内滚动 + 触底懒加载:limit 递增重拉(id desc 排序稳定,天然增量)
      if (taskList.loading) return;
      taskList.loading = true;
      try {
        const rows = await api(`/tasks?limit=${taskList.limit}`);
        recentTasks.value = rows;
        taskList.done = rows.length < taskList.limit;
      } finally {
        taskList.loading = false;
      }
    }
    function onTaskListScroll(e) {
      const el = e.target;
      if (taskList.loading || taskList.done) return;
      if (el.scrollTop + el.clientHeight >= el.scrollHeight - 24) {
        taskList.limit += 10;
        loadTasks();
      }
    }
    async function loadTaskOptions() {
      // 右栏任务下拉候选:跟随浏览区平台筛选(空=全部平台),最近 30 条
      const pf = query.platform ? `&platform=${query.platform}` : "";
      try { taskOptions.value = await api(`/tasks?limit=30${pf}`); } catch (e) {}
    }
    async function pollStatus() {
      try {
        const st = await api("/tasks/status");
        let ended = false;
        for (const [pid, v] of Object.entries(st)) {
          const was = statuses[pid + "_was"];
          if (!v.running && was) {
            ended = true;
            // 任务由运行转为结束:异常中断时弹窗告知原因(风控/访问受限等)
            if (v.last && v.last.status === "error") notifyInterrupted(pid, v.last);
          }
          statuses[pid] = v;
          statuses[pid + "_was"] = v.running;
        }
        if (ended) { reloadJobs(); loadTasks(); loadPlatforms(); loadEvents(); loadTaskOptions(); }
        // 运行中每 15 秒轻量刷新一次数据,让新入库岗位及时可见
        pollTick = (pollTick + 1) % 5;
        if (pollTick === 0 && Object.values(st).some((v) => v.running)) {
          loadJobs(); loadStats(); loadEvents();
        }
      } catch (e) {}
    }
    let pollTick = 0;

    // 任务异常中断弹窗:每平台只对"新出现的错误结果"提示一次
    // (以 任务ID+消息 为指纹;用户手动停止/正常完成不弹)
    const errorNotified = {};
    function notifyInterrupted(pid, last) {
      const sig = `${last.task_id}|${last.message || ""}`;
      if (errorNotified[pid] === sig) return;
      errorNotified[pid] = sig;
      ElMessageBox.alert(
        last.message || "任务异常中断,详情见左下角异常面板。",
        `${platformOf(pid)} 抓取已中断`,
        { confirmButtonText: "知道了", type: "error" }
      );
    }

    function reloadJobs() {
      query.page = 1;
      loadJobs();
      loadStats();
    }
    function setFit(v) {
      query.fit = query.fit === v ? "" : v;
      query.page = 1;
      loadJobs();
    }
    // 表头点击排序:同一列再次点击切换方向;新列默认降序
    function onSortChange({ prop, order }) {
      const byMap = { updated_at: "updated", published_at: "published", hr_active_at: "hr_active", city: "city", experience: "exp_edu" };
      const by = byMap[prop];
      if (!by) return;
      if (query.sort_by === by) {
        query.sort_order = query.sort_order === "desc" ? "asc" : "desc";
      } else {
        query.sort_by = by;
        query.sort_order = order === "ascending" ? "asc" : "desc";
      }
      query.page = 1;
      loadJobs();
    }
    function toggleSortOrder() {
      query.sort_order = query.sort_order === "desc" ? "asc" : "desc";
      query.page = 1;
      loadJobs();
    }

    // 左栏平台切换:只影响抓取上下文,不动右栏筛选(浏览区独立)
    async function switchPlatform(pid) {
      if (grab.platform === pid) return;
      grab.platform = pid;
      grab.filter_id = null;   // 条件按平台隔离
      await loadFilters();
      // 最近任务已全局化(全平台混排),平台切换不再影响任务列表
    }
    function switchMode(m) { grab.mode = m; }

    // ---------- 任务 ↔ 数据 双向关联 ----------
    // 通用跳转(任务详情卡片/岗位详情"来源任务"/右栏任务下拉):
    // 右栏进入"任务视图"——按任务过滤并联动平台。
    // tableId: 任务数据真实归属的工作表(TaskOut.filter_snapshot.table_id /
    // Job.table_id)。进入任务视图时**自动把数据表选择器对齐到该表**:
    // 表选择器永远是数据归属的真值,否则"选着表A看着表B的数据"是交互杂糅的根源
    function viewTaskJobs(taskId, platform, tableId) {
      if (!taskId) return;
      query.task_id = taskId;
      if (platform) query.platform = platform;
      if (tableId !== undefined && tableId !== null) query.table_id = tableId || 1;
      query.page = 1;
      showDetail.value = false;
      taskDetailVisible.value = false;
      reloadJobs();
    }
    // 右栏任务下拉(平台选择框后):v-model 直接绑 query.task_id
    function onTaskPick(tid) {
      if (tid) {
        // 选中任务联动其所属平台与存入表,避免"任务A + 平台B/表C"组合的错位展示
        const t = taskOptions.value.find((x) => x.id === tid);
        const snapTid = t && t.filter_snapshot ? t.filter_snapshot.table_id : undefined;
        viewTaskJobs(tid, t && t.platform, snapTid);
      } else {
        clearTaskFilter();
      }
    }
    function clearTaskFilter() {
      query.task_id = null;
      query.page = 1;
      reloadJobs();
    }
    // 任务视图横幅信息源:任务对象可能来自右栏下拉候选或左栏最近任务
    const activeTask = computed(() => {
      const tid = query.task_id;
      if (!tid) return null;
      return taskOptions.value.find((x) => x.id === tid)
        || recentTasks.value.find((x) => x.id === tid) || null;
    });
    // 右栏平台筛选变化时,任务候选跟随(空=全部平台的任务)
    watch(() => query.platform, () => loadTaskOptions());

    // ---------- 任务管理(删除任务及其全部岗位数据) ----------
    function openTaskDialog() {
      taskMgrVisible.value = true;
      loadTaskOptions();
    }
    async function delTask(t) {
      const n = t.job_count || 0;
      // 无任何关联岗位的任务:不二次确认,直接删除
      if (n > 0) {
        await ElMessageBox.confirm(
          `删除任务 #${t.id}(${taskText(t.status)} ${fmtDate(t.started_at)})?`
          + ` 该任务关联的 ${n} 条岗位数据将一并删除,不可恢复。`,
          "删除任务", { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" }
        );
      }
      try {
        const r = await api(`/tasks/${t.id}`, { method: "DELETE" });
        ElMessage.success(`任务 #${t.id} 已删除` + (r.deleted_jobs ? `,清除岗位 ${r.deleted_jobs} 条` : "(无关联岗位)"));
      } catch (e) {
        return;   // 失败(如运行中):api 层已弹错误提示
      }
      // 删除的是当前正在查看的任务:退出任务过滤视图
      if (query.task_id === t.id) clearTaskFilter();
      loadTaskOptions();   // 弹窗保持打开,列表刷新后可继续删除其它任务
      loadTasks();
      reloadJobs();
    }

    // ---------- 任务控制 ----------
    async function startTask() {
      if (!canStart.value) return;
      const salary = composeSalary(grab.salary_min, grab.salary_max);
      if (salary === "INVALID") {
        ElMessage.warning("薪资范围请填写数字(K/月),单边可留空");
        return;
      }
      starting.value = true;
      try {
        // 抓取前登录预检查:未登录直接拦下并引导登录(不走任务报错)
        const lg = await api("/tasks/check_login", { method: "POST", body: { platform: grab.platform } });
        if (!lg.logged_in) {
          ElMessageBox.alert(
            `${lg.name || platformOf(grab.platform)} 未登录:请在已打开的浏览器窗口中登录后,再点击「开始抓取」。`,
            "未登录", { confirmButtonText: "知道了", type: "warning" }
          );
          return;
        }
        const base = {
          platform: grab.platform,
          salary,
          table_id: grab.table_id || 1,   // 存入目标表(左栏选择,与右栏浏览无关)
          max_pages: grab.max_pages,
        };
        const body = grab.mode === "saved"
          ? { ...base, filter_id: grab.filter_id }
          : {
              ...base,
              gear_id: grab.gear_id || null,
              cities: grab.cities,          // 多选城市(空 = 全国)
            };
        await api("/tasks/start", { method: "POST", body });
        ElMessage.success(`任务已启动,结果存入「${currentTableName.value}」`);
        loadTasks();
      } finally {
        starting.value = false;
      }
    }
    async function stopTask() {
      // 按后端真实返回反馈:任务不存在(ok=false)时如实提示,
      // 避免"已发送停止指令"的假象(界面状态未及时刷新时按钮仍显示停止)
      try {
        const r = await api(`/tasks/${grab.platform}/stop`, { method: "POST", body: {} });
        if (r && r.ok) {
          ElMessage.info("停止指令已发送,当前岗位处理完立即中断");
        } else {
          ElMessage.warning((r && r.msg) || "当前没有运行中的抓取任务");
        }
      } catch (e) {}
    }

    // ---------- 岗位设置管理 ----------
    function openGearDialog() {
      gearVisible.value = true;
      loadGears();
    }
    function resetGearForm() {
      gearForm.id = null; gearForm.name = ""; gearForm.keywords = [];
    }
    function editGear(row) {
      Object.assign(gearForm, { id: row.id, name: row.name, keywords: [...(row.keywords || [])] });
    }
    async function createGear() {
      // 参考数据表新建:先一键建项(名称缺省 → 后端自动命名「新岗位设置_n」),
      // 再对新表项提供行内编辑框;词条可缺省(留空保存即无词条)
      const row = await api("/gears", { method: "POST", body: { name: "", keywords: [] } });
      ElMessage.success("已添加岗位设置");
      await loadGears();
      const item = gears.value.find((g) => g.id === row.id);
      if (item) editGear(item);   // 新表项自动进入行内编辑
    }
    async function saveGear() {
      if (!gearForm.name.trim()) return ElMessage.warning("请填写设置名");
      await api("/gears" + (gearForm.id ? `/${gearForm.id}` : ""), {
        method: gearForm.id ? "PUT" : "POST",
        body: { name: gearForm.name.trim(), keywords: gearForm.keywords },
      });
      ElMessage.success("已保存");
      resetGearForm();
      loadGears();
    }
    async function delGear(id) {
      await ElMessageBox.confirm(
        "删除设置不影响已入库岗位与条件,确定删除?", "删除设置", { type: "warning" }
      );
      await api(`/gears/${id}`, { method: "DELETE" });
      ElMessage.success("已删除");
      loadGears();
    }

    // ---------- 条件管理 ----------
    function openFilterDialog() {
      filterVisible.value = true;
      loadGears();
      loadFilters().then(resetFilterForm);
    }
    function resetFilterForm() {
      filterForm.id = null;
      filterForm.platform = grab.platform;
      filterForm.name = `${platformOf(grab.platform)}-${grab.cities.length ? grab.cities.join("_") : "全国"}`;
      filterForm.cities = [...grab.cities];
      filterForm.gear_id = grab.gear_id || null;
      const [smin, smax] = (grab.salary_min.trim() || grab.salary_max.trim())
        ? [grab.salary_min.trim(), grab.salary_max.trim()] : ["", ""];
      filterForm.salary_min = smin;
      filterForm.salary_max = smax;
      filterForm.industries_avoid = [];
      filterForm.industries_prefer = [];
    }
    function editFilter(row) {
      filterForm.id = row.id;
      filterForm.platform = row.platform;   // 编辑时锁定原始平台,防止被当前界面平台覆盖
      filterForm.name = row.name;
      // 城市串(逗号分隔,兼容旧单值)→ 标签数组
      filterForm.cities = (row.city || "").split(",").map((s) => s.trim()).filter(Boolean);
      filterForm.gear_id = row.gear_id || null;
      const [smin, smax] = (row.extras.salary || "").split(",");
      filterForm.salary_min = smin || "";
      filterForm.salary_max = smax || "";
      filterForm.industries_avoid = [...(row.extras.industries_avoid || [])];
      filterForm.industries_prefer = [...(row.extras.industries_prefer || [])];
    }
    function editSelectedFilter() {
      if (!selectedFilter.value) return;
      editFilter(selectedFilter.value);
      filterVisible.value = true;
    }
    async function saveFilter() {
      if (!filterForm.name.trim()) return ElMessage.warning("请填写条件名");
      // 词条只能来自岗位设置,所以条件必须关联设置(否则启动抓取会因无词条失败)
      if (!filterForm.gear_id) return ElMessage.warning("请选择岗位设置");
      const salary = composeSalary(filterForm.salary_min, filterForm.salary_max);
      if (salary === "INVALID") {
        ElMessage.warning("薪资范围请填写数字(K/月),单边可留空");
        return;
      }
      const body = {
        platform: filterForm.platform,
        name: filterForm.name.trim(),
        city: filterForm.cities.join(","),   // 多选城市存逗号分隔串(兼容旧单值格式)
        gear_id: filterForm.gear_id,
        extras: {
          salary: salary || "",
          industries_avoid: filterForm.industries_avoid,
          industries_prefer: filterForm.industries_prefer,
        },
      };
      await api("/filters" + (filterForm.id ? `/${filterForm.id}` : ""), {
        method: filterForm.id ? "PUT" : "POST",
        body,
      });
      ElMessage.success(filterForm.id ? "条件已更新" : "条件已保存");
      resetFilterForm();
      loadFilters();
    }
    async function delFilter(id) {
      await ElMessageBox.confirm("确定删除该筛选条件?", "删除条件", { type: "warning" });
      await api(`/filters/${id}`, { method: "DELETE" });
      ElMessage.success("已删除");
      if (grab.filter_id === id) grab.filter_id = null;
      loadFilters();
    }

    // ---------- 数据表管理 ----------
    function openTableDialog() {
      tableVisible.value = true;
      resetTableForm();
      loadTables();
    }
    function resetTableForm() {
      tableForm.id = null; tableForm.name = ""; tableForm.slug = "";
    }
    function editTable(row) {
      Object.assign(tableForm, { id: row.id, name: row.name, slug: row.slug });
    }
    async function createTable() {
      // 表名留空 → 后端自动命名「数据表_id」
      await api("/tables", { method: "POST", body: { name: "", slug: "" } });
      ElMessage.success("已新建数据表");
      loadTables();
    }
    async function saveTable() {
      if (!tableForm.name.trim()) return ElMessage.warning("请填写表名");
      await api(`/tables/${tableForm.id}`, {
        method: "PUT",
        body: { name: tableForm.name.trim(), slug: tableForm.slug.trim() },
      });
      ElMessage.success("已保存");
      resetTableForm();
      loadTables();
    }
    async function clearTable(row) {
      await ElMessageBox.confirm(
        `清空数据表「${row.name}」?表内 ${row.job_count || 0} 条岗位将被删除,且不可恢复。`,
        "清空数据表", { type: "warning", confirmButtonText: "清空", cancelButtonText: "取消" }
      );
      const r = await api(`/tables/${row.id}/clear`, { method: "POST", body: {} });
      ElMessage.success(`已清空 ${r.deleted} 条岗位`);
      loadTables();
      if ((query.table_id || 1) === row.id) {
        query.task_id = null;   // 表已清空,任务过滤失去意义
        reloadJobs();
      }
    }
    async function delTable(row) {
      const n = row.job_count || 0;
      // 空表(无任何岗位数据):不二次确认,直接删除
      if (n > 0) {
        await ElMessageBox.confirm(
          `是否要删除表「${row.name}」并清除所有表数据?表内 ${n} 条岗位将一并删除,且不可恢复。`,
          "删除数据表", { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" }
        );
      }
      await api(`/tables/${row.id}`, { method: "DELETE" });
      ElMessage.success("已删除" + (n ? `,清除岗位 ${n} 条` : "(空表)"));
      // 删除的是当前选中的表:自动切回默认表并刷新列表(同时退出任务视图:
      // 任务归属的表已不存在,任务过滤失去意义)
      if ((query.table_id || 1) === row.id) {
        query.table_id = 1;
        query.task_id = null;
        reloadJobs();
      }
      // 删除的是左栏"存入目标表":回退默认表(否则新抓数据写入不存在的表)
      if ((grab.table_id || 1) === row.id) grab.table_id = 1;
      resetTableForm();
      loadTables();
    }
    // 数据表切换:刷新列表(本地记忆由统一 watch 落盘)
    // 任务过滤视图下 table_id 不生效(API 按 task_id 过滤并忽略工作表),
    // 因此切表必须先退出任务过滤,否则表现为"切换数据表内容不变"
    function onTableChange() {
      query.page = 1;
      if (query.task_id) query.task_id = null;
      reloadJobs();
    }
    // ---------- 界面状态本地记忆(下次打开自动恢复) ----------
    // 统一用 watch 落盘:程序性变化(如删任务/删表后的回退)同样被记忆,不留脏值
    watch(() => query.table_id, (v) => {
      localStorage.setItem(TABLE_MEM_KEY, String(v || 1));
    });
    watch(() => grab.table_id, (v) => {
      localStorage.setItem(GRAB_TABLE_KEY, String(v || 1));
    });
    watch(() => query.platform, (v) => {
      localStorage.setItem(VIEW_PLATFORM_KEY, v || "");
    });
    watch(() => query.task_id, (v) => {
      if (v) localStorage.setItem(VIEW_TASK_KEY, String(v));
      else localStorage.removeItem(VIEW_TASK_KEY);
    });

    // ---------- 岗位 ----------
    function openDetail(row) { currentJob.value = row; }
    async function openJob(job) {
      // 经后端在已登录的抓取浏览器窗口打开(保留登录态,非系统默认浏览器)
      try {
        const r = await api(`/jobs/${job.id}/open`, { method: "POST", body: {} });
        if (r.need_login) {
          ElMessageBox.alert(r.msg, "未登录", { confirmButtonText: "知道了", type: "warning" });
        } else if (r.need_verify) {
          ElMessageBox.alert(r.msg, "需要安全验证", {
            confirmButtonText: "知道了", type: "warning",
          });
        } else {
          ElMessage.success(r.msg || "已在已登录浏览器中打开");
        }
      } catch (e) {}
    }
    async function openEvent(e) {
      // 异常面板点击跳转:在已登录浏览器中打开相关页面
      try {
        const r = await api(`/events/${e.id}/open`, { method: "POST", body: {} });
        if (r.need_login) {
          ElMessageBox.alert(r.msg, "未登录", { confirmButtonText: "知道了", type: "warning" });
        } else if (r.need_verify) {
          ElMessageBox.alert(r.msg, "需要安全验证", { confirmButtonText: "知道了", type: "warning" });
        } else {
          ElMessage.success(r.msg || "已在已登录浏览器中打开");
        }
      } catch (err) {}
    }
    async function delJob(job) {
      await ElMessageBox.confirm(
        `确认删除岗位「${job.title || ""}」?`, "删除岗位",
        { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" }
      );
      await api(`/jobs/${job.id}`, { method: "DELETE" });
      ElMessage.success("已删除");
      currentJob.value = null; showDetail.value = false;
      loadJobs(); loadStats();
    }
    function exportJobs() {
      const pid = query.platform ? `?platform=${query.platform}` : "";
      window.open("/api/export" + pid, "_blank");
    }
    function onMenu(cmd) {
      if (cmd === "gears") openGearDialog();
      else if (cmd === "filters") openFilterDialog();
      else if (cmd === "export") exportJobs();
      else if (cmd === "refresh_active") refreshActive();
    }
    async function refreshActive() {
      try {
        await api("/tasks/refresh_active", { method: "POST", body: { platform: grab.platform } });
        ElMessage.success("补全任务已启动,进度见左栏状态区");
        loadTasks();
      } catch (e) {}
    }

    // 搜索防抖:输入即搜
    let qTimer = null;
    watch(() => query.q, () => {
      clearTimeout(qTimer);
      qTimer = setTimeout(() => { query.page = 1; loadJobs(); }, 400);
    });

    // 弹窗关闭即还原编辑/新建状态(未保存的编辑不残留到下次打开)
    watch(gearVisible, (v) => { if (!v) resetGearForm(); });
    watch(tableVisible, (v) => { if (!v) resetTableForm(); });

    watch(currentJob, (v) => { if (v) showDetail.value = true; });

    onMounted(async () => {
      try {
        await api("/health");
        healthText.value = "规则备注已启用";
      } catch (e) {
        healthText.value = "后端未连接";
      }
      await loadPlatforms();
      // 恢复右栏浏览平台(独立记忆;非法值忽略,空=全部平台)
      const viewPlat = localStorage.getItem(VIEW_PLATFORM_KEY) || "";
      if (viewPlat && platforms.value.some((p) => p.id === viewPlat)) {
        query.platform = viewPlat;
      }
      await loadGears();
      await loadTables();
      // 本地记忆:恢复上次浏览的数据表(表已不存在则回默认表)
      const savedTid = parseInt(localStorage.getItem(TABLE_MEM_KEY) || "", 10);
      if (savedTid && tables.value.some((t) => t.id === savedTid)) {
        query.table_id = savedTid;
      }
      // 本地记忆:恢复左栏存入目标表(独立于浏览表;表已不存在则回默认表)
      const savedGTid = parseInt(localStorage.getItem(GRAB_TABLE_KEY) || "", 10);
      if (savedGTid && tables.value.some((t) => t.id === savedGTid)) {
        grab.table_id = savedGTid;
      }
      // 本地记忆:恢复任务过滤。候选加载后校验:不在最近候选中(平台不符/
      // 已删除/超出30条)则放弃恢复,避免打开页面就是空列表
      await loadTaskOptions();
      const savedTask = parseInt(localStorage.getItem(VIEW_TASK_KEY) || "", 10);
      if (savedTask) {
        const t = taskOptions.value.find((x) => x.id === savedTask);
        if (t) {
          query.task_id = savedTask;
          if (t.platform !== query.platform) query.platform = t.platform;
          // 与 viewTaskJobs 同一规则:任务视图下表选择器对齐任务存入表
          query.table_id = (t.filter_snapshot ? t.filter_snapshot.table_id : null) || 1;
        } else {
          localStorage.removeItem(VIEW_TASK_KEY);
        }
      }
      await loadFilters();
      await loadTasks();
      loadEvents();
      reloadJobs();
      await pollStatus();
      timer = setInterval(pollStatus, 3000);
      document.addEventListener("click", onDocClick);
    });
    onUnmounted(() => {
      clearInterval(timer);
      document.removeEventListener("click", onDocClick);
    });

    return {
      SearchIcon, RefreshIcon, LinkIcon, DeleteIcon, SettingIcon,
      platforms, gears, filters, tables, jobs, stats, recentTasks, events, eventsOpen,
      taskOptions, taskMgrVisible, taskList, onTaskListScroll, activeTask,
      grab, query, statuses, healthText, loading, starting,
      currentJob, showDetail, gearVisible, filterVisible, tableVisible,
      taskDetailVisible, detailTask, showTaskDetail, welfareTags,
      jobKeywords, hitKeywords, highlightedDesc, highlightedTitle,
      comboHover, comboCur,
      gearForm, filterForm, tableForm,
      platformName, selectedFilter, currentStatus, running, canStart, startHint,
      sortState, skipTip, currentTableName,
      platformOf, gearName, isRunning, taskStatusOf, fmtTime, fmtDate, fmtSalaryRange, fmtCities, taskTagType, taskText,
      kwCount, cityLabel,
      kwCount, cityLabel,
      platformLogo, onLogoError,
      activeTagType, activeTip, clearEvents, openEvent,
      switchPlatform, switchMode, startTask, stopTask, reloadJobs, setFit,
      viewTaskJobs, clearTaskFilter, onTaskPick, openTaskDialog, delTask,
      loadJobs,
      onSortChange, toggleSortOrder, onTableChange,
      openGearDialog, resetGearForm, editGear, createGear, saveGear, delGear,
      openFilterDialog, resetFilterForm, editFilter, editSelectedFilter, saveFilter, delFilter,
      openTableDialog, resetTableForm, editTable, createTable, saveTable, clearTable, delTable,
      openDetail, openJob, delJob, exportJobs, onMenu,
    };
  },
});
app.use(ElementPlus, { locale: ElementPlusLocaleZhCn });
app.component("KwEditor", KwEditor);
app.component("CityPicker", CityPicker);
for (const [name, comp] of Object.entries(ElementPlusIconsVue)) {
  app.component(name, comp);
}
app.mount("#app");
