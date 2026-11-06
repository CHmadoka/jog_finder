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
const SearchIcon = ElementPlusIconsVue.Search;
const RefreshIcon = ElementPlusIconsVue.Refresh;
const LinkIcon = ElementPlusIconsVue.Link;
const DeleteIcon = ElementPlusIconsVue.Delete;

const app = createApp({
  setup() {
    // ---------- 状态 ----------
    const platforms = ref([]);
    const gears = ref([]);
    const filters = ref([]);
    const tables = ref([]);
    const jobs = reactive({ total: 0, items: [] });
    const stats = reactive({ total: 0, fit: 0, unfit: 0, unknown: 0, online: 0 });
    const recentTasks = ref([]);
    const healthText = ref("检测连接...");
    const loading = ref(false);
    const starting = ref(false);

    // 左栏:抓取配置(只影响"发起抓取")
    const grab = reactive({
      platform: "boss",
      mode: "quick",            // quick | saved
      filter_id: null,
      gear_id: null,
      city: "",
      keyword: "",
      salary: "",               // 薪资范围原文(如 10-20 / 10, / ,20;K/月)
      table_id: 1,              // 新岗位存入的目标数据表
      max_pages: 3,
      all_keywords: true,
    });

    // 右栏:岗位浏览筛选(只影响"看什么",默认全部平台)
    // 默认按最后更新时间倒序;城市/经验学历/HR活跃列可点击分类排列
    const query = reactive({
      platform: "", gear_id: null, table_id: null, fit: "", city: "", q: "",
      sort_by: "updated", sort_order: "desc", page: 1, page_size: 20,
    });

    const statuses = reactive({});   // platform -> { running, progress, counters }
    const currentJob = ref(null);
    const showDetail = ref(false);
    const gearVisible = ref(false);
    const filterVisible = ref(false);
    const tableVisible = ref(false);
    const events = ref([]);
    const eventsOpen = ref(false);
    const gearForm = reactive({ id: null, name: "", keywords: [] });
    const filterForm = reactive({
      id: null, platform: "boss", name: "", city: "", gear_id: null, keyword: "",
      salary: "",
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
      return !!(grab.gear_id || (grab.keyword || "").trim());
    });
    const startHint = computed(() => {
      if (canStart.value) return "";
      if (grab.mode === "saved") return "请先选择一个已存条件";
      return "请先选择岗位档位,或填写限定词条";
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
    function gearName(gid) {
      return (gears.value.find((g) => g.id === gid) || {}).name || "档位#" + gid;
    }
    function isRunning(pid) {
      const s = statuses[pid];
      if (s && s.running !== undefined) return !!s.running;
      const p = platforms.value.find((x) => x.id === pid);
      return !!(p && p.running);
    }
    function fmtTime(s) { return s ? String(s).replace("T", " ").slice(0, 16) : "-"; }
    function fmtDate(s) { return s ? String(s).slice(5, 10) : "-"; }
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
    function taskText(s) {
      return { done: "完成", running: "进行中", stopped: "已停止", error: "失败" }[s] || s;
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
        if (query.gear_id) params.set("gear_id", query.gear_id);
        if (query.table_id) params.set("table_id", query.table_id);
        if (query.fit) params.set("fit", query.fit);
        if (query.city.trim()) params.set("city", query.city.trim());
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
      if (query.gear_id) params.set("gear_id", query.gear_id);
      if (query.table_id) params.set("table_id", query.table_id);
      if (query.city.trim()) params.set("city", query.city.trim());
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
      recentTasks.value = await api(`/tasks?platform=${grab.platform}&limit=5`);
    }
    async function pollStatus() {
      try {
        const st = await api("/tasks/status");
        let ended = false;
        for (const [pid, v] of Object.entries(st)) {
          const was = statuses[pid + "_was"];
          if (!v.running && was) ended = true;
          statuses[pid] = v;
          statuses[pid + "_was"] = v.running;
        }
        if (ended) { reloadJobs(); loadTasks(); loadPlatforms(); loadEvents(); }
        // 运行中每 15 秒轻量刷新一次数据,让新入库岗位及时可见
        pollTick = (pollTick + 1) % 5;
        if (pollTick === 0 && Object.values(st).some((v) => v.running)) {
          loadJobs(); loadStats(); loadEvents();
        }
      } catch (e) {}
    }
    let pollTick = 0;

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

    // 左栏平台切换:只影响抓取上下文,不动右栏筛选
    async function switchPlatform(pid) {
      if (grab.platform === pid) return;
      grab.platform = pid;
      grab.filter_id = null;   // 条件按平台隔离
      await loadFilters();
      loadTasks();
    }
    function switchMode(m) { grab.mode = m; }

    // ---------- 任务控制 ----------
    async function startTask() {
      if (!canStart.value) return;
      starting.value = true;
      try {
        const base = {
          platform: grab.platform,
          salary: grab.salary.trim() || null,
          table_id: grab.table_id || null,
          max_pages: grab.max_pages,
        };
        const body = grab.mode === "saved"
          ? { ...base, filter_id: grab.filter_id }
          : {
              ...base,
              gear_id: grab.gear_id || null,
              city: grab.city.trim() || null,
              keyword: grab.keyword.trim() || null,
              all_keywords: grab.all_keywords,
            };
        await api("/tasks/start", { method: "POST", body });
        ElMessage.success("任务已启动");
        loadTasks();
      } finally {
        starting.value = false;
      }
    }
    async function stopTask() {
      await api(`/tasks/${grab.platform}/stop`, { method: "POST", body: {} });
      ElMessage.info("已发送停止指令");
    }

    // ---------- 档位管理 ----------
    function openGearDialog() {
      gearVisible.value = true;
      loadGears();
    }
    function resetGearForm() {
      gearForm.id = null; gearForm.name = ""; gearForm.keywords = [];
    }
    async function saveGear() {
      if (!gearForm.name.trim()) return ElMessage.warning("请填写档位名");
      await api("/gears", {
        method: gearForm.id ? "PUT" : "POST",
        body: { name: gearForm.name.trim(), keywords: gearForm.keywords },
      });
      ElMessage.success("已保存");
      resetGearForm();
      loadGears();
    }
    async function delGear(id) {
      await ElMessageBox.confirm(
        "删除档位不影响已入库岗位与条件,确定删除?", "删除档位", { type: "warning" }
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
      filterForm.name = `${platformOf(grab.platform)}-${grab.city.trim() || "全国"}`;
      filterForm.city = grab.city.trim();
      filterForm.gear_id = grab.gear_id || null;
      filterForm.keyword = grab.keyword.trim();
      filterForm.salary = grab.salary.trim();
      filterForm.industries_avoid = [];
      filterForm.industries_prefer = [];
    }
    function editFilter(row) {
      filterForm.id = row.id;
      filterForm.platform = row.platform;   // 编辑时锁定原始平台,防止被当前界面平台覆盖
      filterForm.name = row.name;
      filterForm.city = row.city || "";
      filterForm.gear_id = row.gear_id || null;
      filterForm.keyword = row.keyword || "";
      filterForm.salary = row.extras.salary || "";
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
      const body = {
        platform: filterForm.platform,
        name: filterForm.name.trim(),
        city: filterForm.city.trim(),
        gear_id: filterForm.gear_id,
        keyword: filterForm.keyword.trim(),
        extras: {
          salary: filterForm.salary.trim(),
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
      loadTables();
    }
    function resetTableForm() {
      tableForm.id = null; tableForm.name = ""; tableForm.slug = "";
    }
    async function saveTable() {
      if (!tableForm.name.trim()) return ElMessage.warning("请填写表名");
      await api("/tables", {
        method: tableForm.id ? "PUT" : "POST",
        body: { name: tableForm.name.trim(), slug: tableForm.slug.trim() },
      });
      ElMessage.success("已保存");
      resetTableForm();
      loadTables();
    }
    async function delTable(row) {
      await ElMessageBox.confirm(
        `删除数据表「${row.name}」?表内 ${row.job_count || 0} 条岗位将自动归入默认表。`,
        "删除数据表", { type: "warning" }
      );
      await api(`/tables/${row.id}`, { method: "DELETE" });
      ElMessage.success("已删除");
      if (grab.table_id === row.id) grab.table_id = 1;
      if (query.table_id === row.id) query.table_id = null;
      resetTableForm();
      loadTables();
    }

    // ---------- 岗位 ----------
    function openDetail(row) { currentJob.value = row; }
    async function openJob(job) {
      // 经后端在已登录的抓取浏览器窗口打开(保留登录态,非系统默认浏览器)
      try {
        const r = await api(`/jobs/${job.id}/open`, { method: "POST", body: {} });
        if (r.need_verify) {
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
        if (r.need_verify) {
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
    async function clearAll() {
      await ElMessageBox.confirm(
        "将删除全部已抓取岗位数据(档位与条件保留),且不可恢复。确定清空?",
        "清空岗位库", { type: "warning", confirmButtonText: "清空", cancelButtonText: "取消" }
      );
      const r = await api("/jobs/clear_all", { method: "POST", body: {} });
      ElMessage.success(`已清空 ${r.deleted} 条岗位`);
      reloadJobs();
    }
    function onMenu(cmd) {
      if (cmd === "gears") openGearDialog();
      else if (cmd === "filters") openFilterDialog();
      else if (cmd === "tables") openTableDialog();
      else if (cmd === "export") exportJobs();
      else if (cmd === "refresh_active") refreshActive();
      else if (cmd === "clear") clearAll();
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

    watch(currentJob, (v) => { if (v) showDetail.value = true; });

    onMounted(async () => {
      try {
        const h = await api("/health");
        healthText.value = h.llm ? "LLM 备注已启用" : "规则备注(未配置 LLM)";
      } catch (e) {
        healthText.value = "后端未连接";
      }
      await loadPlatforms();
      await loadGears();
      await loadTables();
      await loadFilters();
      await loadTasks();
      loadEvents();
      reloadJobs();
      await pollStatus();
      timer = setInterval(pollStatus, 3000);
    });
    onUnmounted(() => clearInterval(timer));

    return {
      SearchIcon, RefreshIcon, LinkIcon, DeleteIcon,
      platforms, gears, filters, tables, jobs, stats, recentTasks, events, eventsOpen,
      grab, query, statuses, healthText, loading, starting,
      currentJob, showDetail, gearVisible, filterVisible, tableVisible,
      gearForm, filterForm, tableForm,
      platformName, selectedFilter, currentStatus, running, canStart, startHint,
      sortState, skipTip,
      platformOf, gearName, isRunning, fmtTime, fmtDate, taskTagType, taskText,
      activeTagType, activeTip, clearEvents, openEvent,
      switchPlatform, switchMode, startTask, stopTask, reloadJobs, setFit,
      onSortChange, toggleSortOrder,
      openGearDialog, resetGearForm, saveGear, delGear,
      openFilterDialog, resetFilterForm, editFilter, editSelectedFilter, saveFilter, delFilter,
      openTableDialog, resetTableForm, saveTable, delTable,
      openDetail, openJob, delJob, exportJobs, clearAll, onMenu,
    };
  },
});
app.use(ElementPlus, { locale: ElementPlusLocaleZhCn });
for (const [name, comp] of Object.entries(ElementPlusIconsVue)) {
  app.component(name, comp);
}
app.mount("#app");
