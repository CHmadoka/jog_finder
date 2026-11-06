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
const SettingIcon = ElementPlusIconsVue.Setting;
const TABLE_MEM_KEY = "jf_current_table";   // 本地记忆:上次选中的数据表

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
      salary_min: "",           // 薪资范围下限(K/月,可缺省)
      salary_max: "",           // 薪资范围上限(K/月,可缺省)
      max_pages: 3,
      all_keywords: true,
    });

    // 右栏:岗位浏览筛选(默认按最后更新时间倒序)
    // 数据表=当前工作表:既决定看哪个表的岗位,也是新抓岗位的存入目标
    const query = reactive({
      platform: "", table_id: 1, fit: "", q: "",
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
      return !!(grab.gear_id || (grab.keyword || "").trim());
    });
    const startHint = computed(() => {
      if (canStart.value) return "";
      if (grab.mode === "saved") return "请先选择一个已存条件";
      return "请先选择岗位设置,或填写限定词条";
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
    // 当前数据表名(工作表;抓取结果存入该表)
    const currentTableName = computed(() => {
      const t = tables.value.find((x) => x.id === (query.table_id || 1));
      return t ? t.name : "默认表";
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
      return (gears.value.find((g) => g.id === gid) || {}).name || "设置#" + gid;
    }
    function isRunning(pid) {
      const s = statuses[pid];
      if (s && s.running !== undefined) return !!s.running;
      const p = platforms.value.find((x) => x.id === pid);
      return !!(p && p.running);
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
        params.set("table_id", query.table_id || 1);
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
      params.set("table_id", query.table_id || 1);
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
          table_id: query.table_id || 1,
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
        ElMessage.success(`任务已启动,结果存入「${currentTableName.value}」`);
        loadTasks();
      } finally {
        starting.value = false;
      }
    }
    async function stopTask() {
      await api(`/tasks/${grab.platform}/stop`, { method: "POST", body: {} });
      ElMessage.info("已发送停止指令");
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
      filterForm.name = `${platformOf(grab.platform)}-${grab.city.trim() || "全国"}`;
      filterForm.city = grab.city.trim();
      filterForm.gear_id = grab.gear_id || null;
      filterForm.keyword = grab.keyword.trim();
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
      filterForm.city = row.city || "";
      filterForm.gear_id = row.gear_id || null;
      filterForm.keyword = row.keyword || "";
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
      const salary = composeSalary(filterForm.salary_min, filterForm.salary_max);
      if (salary === "INVALID") {
        ElMessage.warning("薪资范围请填写数字(K/月),单边可留空");
        return;
      }
      const body = {
        platform: filterForm.platform,
        name: filterForm.name.trim(),
        city: filterForm.city.trim(),
        gear_id: filterForm.gear_id,
        keyword: filterForm.keyword.trim(),
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
      if ((query.table_id || 1) === row.id) reloadJobs();
    }
    async function delTable(row) {
      await ElMessageBox.confirm(
        `删除数据表「${row.name}」?表内 ${row.job_count || 0} 条岗位将自动归入默认表。`,
        "删除数据表", { type: "warning" }
      );
      await api(`/tables/${row.id}`, { method: "DELETE" });
      ElMessage.success("已删除");
      if ((query.table_id || 1) === row.id) query.table_id = 1;
      resetTableForm();
      loadTables();
    }
    // 数据表切换:刷新列表(本地记忆由统一 watch 落盘)
    function onTableChange() {
      query.page = 1;
      reloadJobs();
    }
    // 当前数据表任何变化都本地记忆,下次打开自动恢复
    watch(() => query.table_id, (v) => {
      localStorage.setItem(TABLE_MEM_KEY, String(v || 1));
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
      // 本地记忆:恢复上次选中的数据表(表已不存在则回默认表)
      const savedTid = parseInt(localStorage.getItem(TABLE_MEM_KEY) || "", 10);
      if (savedTid && tables.value.some((t) => t.id === savedTid)) {
        query.table_id = savedTid;
      }
      await loadFilters();
      await loadTasks();
      loadEvents();
      reloadJobs();
      await pollStatus();
      timer = setInterval(pollStatus, 3000);
    });
    onUnmounted(() => clearInterval(timer));

    return {
      SearchIcon, RefreshIcon, LinkIcon, DeleteIcon, SettingIcon,
      platforms, gears, filters, tables, jobs, stats, recentTasks, events, eventsOpen,
      grab, query, statuses, healthText, loading, starting,
      currentJob, showDetail, gearVisible, filterVisible, tableVisible,
      gearForm, filterForm, tableForm,
      platformName, selectedFilter, currentStatus, running, canStart, startHint,
      sortState, skipTip, currentTableName,
      platformOf, gearName, isRunning, fmtTime, fmtDate, fmtSalaryRange, taskTagType, taskText,
      activeTagType, activeTip, clearEvents, openEvent,
      switchPlatform, switchMode, startTask, stopTask, reloadJobs, setFit,
      onSortChange, toggleSortOrder, onTableChange,
      openGearDialog, resetGearForm, editGear, saveGear, delGear,
      openFilterDialog, resetFilterForm, editFilter, editSelectedFilter, saveFilter, delFilter,
      openTableDialog, resetTableForm, editTable, createTable, saveTable, clearTable, delTable,
      openDetail, openJob, delJob, exportJobs, onMenu,
    };
  },
});
app.use(ElementPlus, { locale: ElementPlusLocaleZhCn });
for (const [name, comp] of Object.entries(ElementPlusIconsVue)) {
  app.component(name, comp);
}
app.mount("#app");
