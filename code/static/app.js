/* Gateway API UI — Alpine component. Defined before Alpine boots (non-deferred). */
function gatewayUI() {
  return {
    // ---- state ----
    view: location.hash.replace('#', '') || 'overview',
    namespace: localStorage.getItem('ns') || 'all',
    routeType: 'all',
    search: '',
    loading: false,
    loaded: false,
    auto: localStorage.getItem('auto') === '1',
    interval: 15,
    dark: document.documentElement.classList.contains('dark'),
    toast: '',

    ctx: { connected: false },
    me: { authEnabled: false, authenticated: true, allowed: true, name: '', email: '', username: '', groups: [], allowedGroups: [], logoutUrl: '/logout' },
    counts: { gatewayClasses: 0, gateways: 0, routes: 0 },
    overview: {},
    gatewayClasses: [],
    gateways: [],
    routes: [],
    routesPage: { items: [], total: 0, offset: 0, limit: 60, loading: false, hasMore: false },
    aiRoutes: [],
    aiAvailable: false,
    policies: [],
    policiesAvailable: false,
    providers: [],
    health: { gateways: {}, routes: {} },
    addrExpanded: {},
    addrLimit: 6,
    graph: { nodes: [], edges: [] },
    namespaces: [],

    hoverId: null,
    related: new Set(),
    drawer: { open: false, tab: 'summary', kind: '', name: '', namespace: '', yaml: '', raw: null, loading: false, related: [], relatedLoading: false,
              charts: { loading: false, window: 30, loaded: false, rps: [], p95Ms: [], errorRate: [] } },
    routeMetrics: {},
    editor: { open: false, mode: 'create', tab: 'form', title: '', form: null, formAvailable: true, yaml: '', busy: false, error: '', result: null },
    formNamespaces: [],
    formServices: { ns: '', items: [], loading: false },
    formGateways: [],
    confirm: { open: false, target: null, busy: false },
    palette: { open: false, q: '', sel: 0, items: [] },
    nsPicker: { open: false, q: '', sel: 0 },
    lastFocused: null,

    _timer: null,

    // ---- lifecycle ----
    async init() {
      window.addEventListener('hashchange', () => { this.view = location.hash.replace('#', '') || 'overview'; this.reload(); });
      // Global ⌘K / Ctrl+K to open the command palette (works regardless of focus).
      window.addEventListener('keydown', (e) => {
        if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) { e.preventDefault(); this.openPalette(); }
      });
      await this.loadMe();
      if (this.accessDenied()) { this.loaded = true; this.renderIcons(); return; }
      await this.loadContext();
      await this.loadNamespaces();
      await this.reload(true);
      this.setupAuto();
    },

    renderIcons() { this.$nextTick(() => window.lucide && window.lucide.createIcons()); },

    go(v) { this.view = v; location.hash = v; this.reload(); },
    title() {
      return { overview: 'Overview', topology: 'Topology', gatewayclasses: 'Gateway Classes',
        gateways: 'Gateways', routes: 'Routes', policies: 'Policies', ai: 'AI Gateway' }[this.view] || 'Gateway API';
    },

    // ---- data loading ----
    async api(path) {
      const r = await fetch(path);
      if (r.status === 401 || r.status === 403) {
        // Authorization changed under us — flip into the access-denied screen.
        this.me.authEnabled = true;
        this.me.authenticated = r.status !== 401;
        this.me.allowed = false;
        throw new Error('forbidden');
      }
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
      return r.json();
    },

    async loadMe() {
      try { this.me = await this.api('/api/me'); } catch (e) { /* stays open by default */ }
    },
    accessDenied() { return this.me.authEnabled && (!this.me.authenticated || !this.me.allowed); },
    logout() { window.location.href = this.me.logoutUrl || '/logout'; },
    userInitials() {
      const s = (this.me.name || this.me.username || this.me.email || '?').trim();
      const parts = s.split(/[\s._-]+/).filter(Boolean);
      return ((parts[0] || '?')[0] + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase();
    },

    async loadContext() {
      try { this.ctx = await this.api('/api/context'); }
      catch (e) { this.ctx = { connected: false, error: String(e.message || e) }; }
    },

    async loadNamespaces() {
      try {
        this.namespaces = (await this.api('/api/namespaces')).namespaces || [];
        // Drop a stale persisted namespace that no longer has any resources.
        if (this.namespace !== 'all' && !this.namespaces.includes(this.namespace)) {
          this.namespace = 'all'; localStorage.setItem('ns', 'all');
        }
      } catch (e) { /* ignore — 'all' still works */ }
    },

    qs() { return this.namespace && this.namespace !== 'all' ? `?namespace=${encodeURIComponent(this.namespace)}` : ''; },

    async reload(force) {
      if (this.loading) return;
      this.loading = true;
      localStorage.setItem('ns', this.namespace);
      if (force) { try { await fetch('/api/refresh', { method: 'POST' }); } catch (e) {} }
      try {
        const q = this.qs();
        if (this.view === 'overview') {
          this.overview = await this.api('/api/overview' + q);
          this.counts = { ...this.counts, ...this.overview.counts };
          this.health = this.overview.health || { gateways: {}, routes: {} };
        } else if (this.view === 'gatewayclasses') {
          this.gatewayClasses = (await this.api('/api/gatewayclasses')).items;
          this.counts.gatewayClasses = this.gatewayClasses.length;
        } else if (this.view === 'gateways') {
          this.gateways = (await this.api('/api/gateways' + q)).items;
          this.counts.gateways = this.gateways.length;
        } else if (this.view === 'routes') {
          await this.loadRoutes(true);
        } else if (this.view === 'ai') {
          this.aiRoutes = (await this.api('/api/ai/routes' + q)).items;
        } else if (this.view === 'policies') {
          const p = await this.api('/api/policies' + q);
          this.policies = p.items; this.counts.policies = p.items.length;
          this.policiesAvailable = p.anyAvailable;
        } else if (this.view === 'topology') {
          this.graph = await this.api('/api/graph' + q);
        }
        // keep sidebar counts roughly fresh & AI tab visibility
        this.refreshSidebarCounts();
      } catch (e) {
        this.notify('Load failed: ' + (e.message || e));
      } finally {
        this.loading = false; this.loaded = true;
        this.renderIcons();
        if (this.view === 'topology') this.$nextTick(() => this.drawEdges());
      }
    },

    async refreshSidebarCounts() {
      // keeps the sidebar (counts, health dots, providers) fresh on every view;
      // all endpoints are server-side cached so this is cheap.
      try {
        const o = await this.api('/api/overview' + this.qs());
        this.counts = { ...this.counts, ...o.counts };
        this.health = o.health || { gateways: {}, routes: {} };
        const ai = await this.api('/api/ai/routes' + this.qs());
        this.aiRoutes = this.view === 'ai' ? this.aiRoutes : ai.items;
        this.aiAvailable = ai.available;
        const pol = await this.api('/api/policies' + this.qs());
        this.policiesAvailable = pol.anyAvailable;
        this.counts.policies = pol.items.length;
        if (this.view !== 'policies') this.policies = pol.items;
        this.providers = (await this.api('/api/providers')).items || [];
        this.renderIcons();
      } catch (e) {}
    },

    setupAuto() {
      clearInterval(this._timer);
      if (this.auto) this._timer = setInterval(() => this.reload(), this.interval * 1000);
    },
    toggleAuto() { this.auto = !this.auto; localStorage.setItem('auto', this.auto ? '1' : '0'); this.setupAuto(); this.notify(this.auto ? `Auto-refresh every ${this.interval}s` : 'Auto-refresh off'); },
    toggleTheme() {
      this.dark = !this.dark;
      document.documentElement.classList.toggle('dark', this.dark);
      localStorage.setItem('theme', this.dark ? 'dark' : 'light');
      this.renderIcons();
      if (this.view === 'topology') this.$nextTick(() => this.drawEdges());
    },

    // ---- filtering / helpers ----
    filtered(list) {
      const s = this.search.trim().toLowerCase();
      if (!s) return list;
      return list.filter(o => JSON.stringify(o).toLowerCase().includes(s));
    },
    filteredRoutes() {
      let r = this.routes;
      if (this.routeType !== 'all') r = r.filter(x => x.routeType === this.routeType);
      return this.filtered(r);
    },
    policyGroups() {
      const order = ['SecurityPolicy', 'ClientTrafficPolicy', 'BackendTrafficPolicy', 'BackendSecurityPolicy'];
      const items = this.filtered(this.policies);
      return order.map(kind => ({ kind, items: items.filter(p => p.kind === kind) }));
    },
    navHealth(kind) { const h = this.health[kind] || {}; if ((h.error || 0) > 0) return 'error'; if ((h.warn || 0) > 0) return 'warn'; return ''; },
    navProblems(kind) { const h = this.health[kind] || {}; return (h.error || 0) + (h.warn || 0); },
    healthTotals() {
      const g = this.health.gateways || {}, r = this.health.routes || {};
      return { ok: (g.ok || 0) + (r.ok || 0), warn: (g.warn || 0) + (r.warn || 0), error: (g.error || 0) + (r.error || 0) };
    },
    isAddrExpanded(key) { return !!this.addrExpanded[key]; },
    toggleAddr(key) { this.addrExpanded[key] = !this.addrExpanded[key]; this.renderIcons(); },
    addrShown(g) {
      const a = g.addresses || [];
      const key = g.namespace + '/' + g.name;
      return (this.addrExpanded[key] || a.length <= this.addrLimit) ? a : a.slice(0, this.addrLimit);
    },
    healthIcon(h) { return { ok: 'check-circle', warn: 'alert-circle', error: 'x-circle', unknown: 'help-circle' }[h] || 'help-circle'; },
    healthVar(h) { return h || 'unknown'; },
    healthyPct() {
      const g = this.overview.health?.gateways || {}, r = this.overview.health?.routes || {};
      const tot = (g.ok || 0) + (g.warn || 0) + (g.error || 0) + (g.unknown || 0) + (r.ok || 0) + (r.warn || 0) + (r.error || 0) + (r.unknown || 0);
      if (!tot) return 100;
      return Math.round(((g.ok || 0) + (r.ok || 0)) / tot * 100);
    },

    // ---- detail drawer ----
    DRAWER_KINDS: ['GatewayClass', 'Gateway', 'HTTPRoute', 'GRPCRoute', 'TLSRoute', 'TCPRoute', 'AIGatewayRoute', 'SecurityPolicy', 'ClientTrafficPolicy', 'BackendTrafficPolicy', 'BackendSecurityPolicy'],
    isDrawerKind(kind) { return this.DRAWER_KINDS.includes(kind); },
    async openObj(o) {
      if (!o || !o.kind) return;
      if (!this.DRAWER_KINDS.includes(o.kind)) {
        this.notify(o.kind + ' ' + (o.namespace ? o.namespace + '/' : '') + o.name + ' — not a Gateway API object');
        return;
      }
      if (!this.drawer.open) this.lastFocused = document.activeElement;
      let keepTab = this.drawer.open ? this.drawer.tab : 'summary';
      const isRoute = this.routeKinds.includes(o.kind);
      if (keepTab === 'metrics' && !(isRoute && this.ctx.metricsEnabled)) keepTab = 'summary';
      this.drawer = { open: true, tab: keepTab, kind: o.kind, name: o.name, namespace: o.namespace || '', yaml: '', raw: null, loading: true, related: [], relatedLoading: true,
                      charts: { loading: false, window: this.drawer.charts ? this.drawer.charts.window : 30, loaded: false, rps: [], p95Ms: [], errorRate: [] } };
      this.renderIcons();
      if (keepTab === 'metrics') this.loadCharts();
      this.$nextTick(() => this.$refs.drawerClose && this.$refs.drawerClose.focus());
      const q = o.namespace ? `&namespace=${encodeURIComponent(o.namespace)}` : '';
      const base = `kind=${encodeURIComponent(o.kind)}&name=${encodeURIComponent(o.name)}${q}`;
      try {
        const d = await this.api('/api/object?' + base);
        this.drawer.raw = d.raw; this.drawer.yaml = d.yaml;
      } catch (e) { this.drawer.yaml = '# ' + (e.message || e); }
      finally { this.drawer.loading = false; this.renderIcons(); }
      // related loads in the background so the Related tab is ready when clicked
      this.api('/api/related?' + base)
        .then(d => { this.drawer.related = d.items; })
        .catch(() => { this.drawer.related = []; })
        .finally(() => { this.drawer.relatedLoading = false; this.renderIcons(); });
    },
    drawerConds() {
      // routes carry conditions under status.parents[].conditions
      const parents = this.drawer.raw?.status?.parents || [];
      return parents.flatMap(p => (p.conditions || []).map(c => ({ ...c, controller: p.controllerName })));
    },
    drawerIsRoute() { return this.routeKinds.includes(this.drawer.kind); },
    drawerTab(tab) {
      this.drawer.tab = tab; this.renderIcons();
      if (tab === 'metrics' && !this.drawer.charts.loaded) this.loadCharts();
    },
    async loadCharts() {
      const d = this.drawer;
      if (!this.ctx.metricsEnabled || !this.drawerIsRoute()) return;
      d.charts.loading = true;
      try {
        const p = new URLSearchParams({ namespace: d.namespace, name: d.name, window: d.charts.window });
        const r = await this.api('/api/metrics/route?' + p.toString());
        d.charts.rps = r.rps || []; d.charts.p95Ms = r.p95Ms || []; d.charts.errorRate = r.errorRate || [];
        d.charts.loaded = true;
      } catch (e) { d.charts.rps = []; d.charts.p95Ms = []; d.charts.errorRate = []; }
      finally { d.charts.loading = false; this.renderIcons(); }
    },
    setChartWindow(w) { this.drawer.charts.window = w; this.loadCharts(); },
    chartCurrent(pts, fmt) { return (pts && pts.length) ? fmt(pts[pts.length - 1][1]) : '—'; },
    // ---- SVG time-series chart (no chart lib) ----
    _niceMax(v) {
      if (v <= 0) return 1;
      const pow = Math.pow(10, Math.floor(Math.log10(v)));
      const n = v / pow;
      const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
      return step * pow;
    },
    _hhmm(ts) { const d = new Date(ts * 1000); return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0'); },
    chartSvg(points, opts = {}) {
      const fmt = opts.fmt || (v => '' + Math.round(v));
      if (!points || points.length < 2) return '<div class="chart-empty">no data in this window</div>';
      const W = 560, H = 150, padL = 46, padR = 10, padT = 10, padB = 20;
      const color = opts.color || '#6366f1';
      const t0 = points[0][0], t1 = points[points.length - 1][0];
      let ymax = this._niceMax(Math.max(...points.map(p => p[1]), opts.floor || 0));
      const plotW = W - padL - padR, plotH = H - padT - padB;
      const X = t => padL + ((t - t0) / (t1 - t0 || 1)) * plotW;
      const Y = v => padT + plotH - (v / ymax) * plotH;
      const line = points.map((p, i) => (i ? 'L' : 'M') + X(p[0]).toFixed(1) + ' ' + Y(p[1]).toFixed(1)).join(' ');
      const area = `M${X(t0).toFixed(1)} ${(padT + plotH).toFixed(1)} ` +
        points.map(p => `L${X(p[0]).toFixed(1)} ${Y(p[1]).toFixed(1)}`).join(' ') +
        ` L${X(t1).toFixed(1)} ${(padT + plotH).toFixed(1)} Z`;
      let grid = '';
      [0, 0.5, 1].forEach(f => {
        const v = ymax * f, y = Y(v).toFixed(1);
        grid += `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}" class="chart-grid"/>` +
          `<text x="${padL - 6}" y="${(+y + 3).toFixed(1)}" class="chart-ytick">${fmt(v)}</text>`;
      });
      let xl = '';
      [[t0, 'start'], [(t0 + t1) / 2, 'middle'], [t1, 'end']].forEach(([t, a]) => {
        xl += `<text x="${X(t).toFixed(1)}" y="${H - 5}" text-anchor="${a}" class="chart-xtick">${this._hhmm(t)}</text>`;
      });
      const uid = 'g' + Math.random().toString(36).slice(2, 7);
      return `<svg viewBox="0 0 ${W} ${H}" class="chart-svg" role="img">` +
        `<defs><linearGradient id="${uid}" x1="0" y1="0" x2="0" y2="1">` +
        `<stop offset="0" stop-color="${color}" stop-opacity="0.22"/><stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>` +
        grid + `<path d="${area}" fill="url(#${uid})"/><path d="${line}" fill="none" stroke="${color}" stroke-width="1.6" stroke-linejoin="round"/>` +
        xl + `</svg>`;
    },
    fmtAxisRps(v) { return v >= 100 ? Math.round(v) : (v >= 1 ? v.toFixed(1) : v.toFixed(2)); },
    fmtAxisMs(v) { return Math.round(v) + 'ms'; },
    fmtAxisPct(v) { return (v * 100).toFixed(v >= 0.1 ? 0 : 1) + '%'; },
    openNode(n) {
      if (!n.ref || !n.ref.kind || n.ref.kind === 'Service') {
        this.notify(n.ref?.kind === 'Service' ? 'Backend Service: ' + n.label : n.label);
        return;
      }
      this.openObj(n.ref);
    },

    // ---- paginated routes (server-side slim + search) ----
    async loadRoutes(reset) {
      if (this.routesPage.loading) return;
      this.routesPage.loading = true;
      if (reset) { this.routesPage.offset = 0; this.routesPage.items = []; this.routeMetrics = {}; }
      try {
        const p = new URLSearchParams();
        p.set('limit', this.routesPage.limit);
        p.set('offset', this.routesPage.offset);
        if (this.namespace !== 'all') p.set('namespace', this.namespace);
        if (this.routeType !== 'all') p.set('type', this.routeType);
        if (this.search.trim()) p.set('q', this.search.trim());
        const d = await this.api('/api/routes?' + p.toString());
        this.routesPage.items = reset ? d.items : this.routesPage.items.concat(d.items);
        this.routesPage.total = d.total;
        this.routesPage.hasMore = d.hasMore;
        this.routesPage.offset += d.items.length;
        this.counts.routes = d.total;
        this.loadRouteMetricsFor(d.items);
      } catch (e) {
        this.notify('Load failed: ' + (e.message || e));
      } finally { this.routesPage.loading = false; this.renderIcons(); }
    },
    loadMoreRoutes() {
      if (this.routesPage.hasMore && !this.routesPage.loading) this.loadRoutes(false);
    },
    onContentScroll(e) {
      if (this.view !== 'routes' || !this.routesPage.hasMore || this.routesPage.loading) return;
      const el = e.target;
      if (el.scrollTop + el.clientHeight >= el.scrollHeight - 320) this.loadMoreRoutes();
    },

    // ---- route traffic metrics (Prometheus), scoped to the visible page ----
    async loadRouteMetricsFor(items) {
      if (!this.ctx.metricsEnabled || !items || !items.length) return;
      const keys = items.map(r => r.namespace + '/' + r.name).join(',');
      try {
        const d = await this.api('/api/metrics/routes?keys=' + encodeURIComponent(keys));
        this.routeMetrics = { ...this.routeMetrics, ...(d.items || {}) };
        this.renderIcons();
      } catch (e) { /* metrics are best-effort */ }
    },
    routeMx(r) { return this.routeMetrics[r.namespace + '/' + r.name] || null; },
    fmtRps(v) { return v >= 100 ? Math.round(v) : (v >= 10 ? v.toFixed(1) : v.toFixed(2)); },
    fmtErr(v) { return (v * 100).toFixed(v >= 0.1 ? 0 : 1) + '%'; },
    sparkPoints(pts, w = 80, h = 22) {
      if (!pts || pts.length < 2) return '';
      const max = Math.max(...pts, 0.0001), n = pts.length;
      return pts.map((v, i) => {
        const x = (i / (n - 1)) * w;
        const y = h - (v / max) * (h - 2) - 1;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      }).join(' ');
    },

    // ---- write mode: create / edit / delete ----
    routeKinds: ['HTTPRoute', 'GRPCRoute', 'TLSRoute', 'TCPRoute'],
    routeHasHostnames(kind) { return kind !== 'TCPRoute'; },
    routeHasPath(kind) { return kind === 'HTTPRoute'; },
    blankRule() {
      return { path: { type: 'PathPrefix', value: '/' }, backends: [{ name: '', port: 80, weight: '' }],
               rewritePrefix: '', setHeaders: [], timeoutRequest: '', timeoutBackend: '' };
    },
    blankForm() {
      return {
        kind: 'HTTPRoute',
        name: 'my-route',
        namespace: this.namespace !== 'all' ? this.namespace : 'default',
        parent: { name: '', namespace: '', sectionName: '' },
        hostnames: [''],
        rules: [this.blankRule()],
      };
    },
    formToYaml() {
      const f = this.editor.form, L = [];
      const kind = f.kind || 'HTTPRoute';
      const apiv = (kind === 'TLSRoute' || kind === 'TCPRoute') ? 'v1alpha2' : 'v1';
      const isHttp = kind === 'HTTPRoute';
      L.push('apiVersion: gateway.networking.k8s.io/' + apiv, 'kind: ' + kind, 'metadata:');
      L.push('  name: ' + (f.name || 'my-route'));
      L.push('  namespace: ' + (f.namespace || 'default'));
      L.push('spec:');
      if (f.parent.name) {
        L.push('  parentRefs:', '    - name: ' + f.parent.name);
        if (f.parent.namespace) L.push('      namespace: ' + f.parent.namespace);
        if (f.parent.sectionName) L.push('      sectionName: ' + f.parent.sectionName);
      }
      if (this.routeHasHostnames(kind)) {
        const hosts = (f.hostnames || []).map(h => h.trim()).filter(Boolean);
        if (hosts.length) { L.push('  hostnames:'); hosts.forEach(h => L.push('    - ' + h)); }
      }
      L.push('  rules:');
      (f.rules && f.rules.length ? f.rules : [this.blankRule()]).forEach(rule => {
        const r = [];  // relative lines (rule-item prefix added at the end)
        if (isHttp) r.push('matches:', '  - path:', '      type: ' + rule.path.type, '      value: ' + (rule.path.value || '/'));
        const filters = [];
        if (rule.rewritePrefix) filters.push('- type: URLRewrite', '  urlRewrite:', '    path:', '      type: ReplacePrefixMatch', '      replacePrefixMatch: ' + rule.rewritePrefix);
        const setH = (rule.setHeaders || []).filter(h => h.name && h.name.trim());
        if (setH.length) {
          filters.push('- type: RequestHeaderModifier', '  requestHeaderModifier:', '    set:');
          setH.forEach(h => filters.push('      - name: ' + h.name, '        value: ' + (h.value ?? '')));
        }
        if (filters.length) { r.push('filters:'); filters.forEach(l => r.push('  ' + l)); }
        if (rule.timeoutRequest || rule.timeoutBackend) {
          r.push('timeouts:');
          if (rule.timeoutRequest) r.push('  request: ' + rule.timeoutRequest);
          if (rule.timeoutBackend) r.push('  backendRequest: ' + rule.timeoutBackend);
        }
        const backs = (rule.backends || []).filter(b => b.name && b.name.trim());
        const list = backs.length ? backs : [{ name: 'my-service', port: 80 }];
        r.push('backendRefs:');
        list.forEach(b => {
          r.push('  - name: ' + b.name, '    port: ' + (b.port || 80));
          if (b.weight !== '' && b.weight != null && backs.length > 1) r.push('    weight: ' + b.weight);
        });
        r.forEach((ln, i) => L.push((i ? '      ' : '    - ') + ln));
      });
      return L.join('\n') + '\n';
    },
    addHostname() { this.editor.form.hostnames.push(''); },
    removeHostname(i) { this.editor.form.hostnames.splice(i, 1); if (!this.editor.form.hostnames.length) this.editor.form.hostnames.push(''); },
    addRule() { this.editor.form.rules.push(this.blankRule()); this.renderIcons(); },
    removeRule(ri) { this.editor.form.rules.splice(ri, 1); if (!this.editor.form.rules.length) this.addRule(); this.renderIcons(); },
    addRuleBackend(ri) { this.editor.form.rules[ri].backends.push({ name: '', port: 80, weight: '' }); this.renderIcons(); },
    removeRuleBackend(ri, bi) { const bs = this.editor.form.rules[ri].backends; bs.splice(bi, 1); if (!bs.length) bs.push({ name: '', port: 80, weight: '' }); this.renderIcons(); },
    addSetHeader(ri) { this.editor.form.rules[ri].setHeaders.push({ name: '', value: '' }); this.renderIcons(); },
    removeSetHeader(ri, hi) { this.editor.form.rules[ri].setHeaders.splice(hi, 1); this.renderIcons(); },
    editorSetTab(tab) {
      if (tab === 'form' && this.editor.mode === 'edit') this.editor.form = this.objToForm(this.editor.raw);
      if (tab === 'yaml' && this.editor.tab === 'form') this.editor.yaml = this.formToYaml();
      this.editor.tab = tab; this.renderIcons();
      if (tab === 'form') this.ensureFormServices();
    },
    // ---- namespace / service pickers ----
    async loadFormNamespaces() {
      try { this.formNamespaces = (await this.api('/api/namespaces/all')).namespaces || []; }
      catch (e) { this.formNamespaces = this.namespaces.slice(); }
    },
    async ensureFormServices() {
      const ns = this.editor.form && this.editor.form.namespace;
      if (!ns || this.formServices.ns === ns) return;
      this.formServices = { ns, items: [], loading: true };
      try { this.formServices.items = (await this.api('/api/services?namespace=' + encodeURIComponent(ns))).items || []; }
      catch (e) { this.formServices.items = []; }
      finally { this.formServices.loading = false; this.renderIcons(); }
    },
    comboStyle(rect) {
      // position the dropdown with position:fixed so it escapes the modal's
      // overflow clipping; flip above the input when there's no room below.
      if (!rect) return '';
      const below = window.innerHeight - rect.bottom;
      const base = `position:fixed;left:${Math.round(rect.left)}px;width:${Math.round(rect.width)}px;right:auto;z-index:90;`;
      if (below < 220 && rect.top > below) {
        return base + `bottom:${Math.round(window.innerHeight - rect.top + 4)}px;top:auto;`;
      }
      return base + `top:${Math.round(rect.bottom + 4)}px;`;
    },
    serviceNames() { return this.formServices.items.map(s => s.name); },
    servicePorts(name) { const s = this.formServices.items.find(x => x.name === name); return s ? s.ports : []; },
    // ---- gateway / listener pickers ----
    async loadFormGateways() {
      try {
        const items = (await this.api('/api/gateways')).items || [];
        this.formGateways = items.map(g => ({ name: g.name, namespace: g.namespace,
          listeners: (g.listeners || []).map(l => l.name).filter(Boolean) }));
      } catch (e) { this.formGateways = []; }
    },
    gatewayNameOptions() { return [...new Set(this.formGateways.map(g => g.name))]; },
    gatewayNsOptions() {
      const n = this.editor.form && this.editor.form.parent.name;
      const matched = this.formGateways.filter(g => !n || g.name === n).map(g => g.namespace);
      return [...new Set(matched.length ? matched : this.formGateways.map(g => g.namespace))];
    },
    listenerOptions() {
      const p = (this.editor.form && this.editor.form.parent) || {};
      const g = this.formGateways.find(x => x.name === p.name && x.namespace === p.namespace)
             || this.formGateways.find(x => x.name === p.name);
      return g ? g.listeners : [];
    },
    pickGatewayName(name) {
      const p = this.editor.form.parent;
      p.name = name;
      const matches = this.formGateways.filter(g => g.name === name);
      if (matches.length === 1) p.namespace = matches[0].namespace;
      if (p.sectionName && !this.listenerOptions().includes(p.sectionName)) p.sectionName = '';
    },
    pickService(ri, bi, name) {
      this.editor.form.rules[ri].backends[bi].name = name;
      const ports = this.servicePorts(name);
      if (ports.length) this.editor.form.rules[ri].backends[bi].port = ports[0];
    },
    objToForm(raw) {
      const spec = (raw && raw.spec) || {}, m = (raw && raw.metadata) || {};
      const p = (spec.parentRefs || [])[0] || {};
      const rules = (spec.rules || []).map(r => {
        const path = ((r.matches || [])[0] || {}).path || { type: 'PathPrefix', value: '/' };
        let rewritePrefix = '', setHeaders = [];
        (r.filters || []).forEach(f => {
          if (f.type === 'URLRewrite' && f.urlRewrite?.path?.type === 'ReplacePrefixMatch') rewritePrefix = f.urlRewrite.path.replacePrefixMatch || '';
          if (f.type === 'RequestHeaderModifier' && f.requestHeaderModifier?.set) setHeaders = f.requestHeaderModifier.set.map(h => ({ name: h.name, value: h.value ?? '' }));
        });
        const backs = (r.backendRefs || []).map(b => ({ name: b.name || '', port: b.port || 80, weight: b.weight ?? '' }));
        return {
          path: { type: path.type || 'PathPrefix', value: path.value || '/' },
          backends: backs.length ? backs : [{ name: '', port: 80, weight: '' }],
          rewritePrefix, setHeaders,
          timeoutRequest: (r.timeouts || {}).request || '',
          timeoutBackend: (r.timeouts || {}).backendRequest || '',
        };
      });
      return {
        kind: (raw && raw.kind) || 'HTTPRoute',
        name: m.name || '', namespace: m.namespace || 'default',
        parent: { name: p.name || '', namespace: p.namespace || '', sectionName: p.sectionName || '' },
        hostnames: (spec.hostnames && spec.hostnames.length) ? spec.hostnames.slice() : [''],
        rules: rules.length ? rules : [this.blankRule()],
      };
    },
    formRepresentable(raw) {
      // the form models rules with a single match and only URLRewrite(ReplacePrefixMatch)
      // + RequestHeaderModifier(set) filters + timeouts. Anything else -> YAML only.
      if (!raw || !this.routeKinds.includes(raw.kind)) return false;
      for (const r of ((raw.spec || {}).rules || [])) {
        if ((r.matches || []).length > 1) return false;
        for (const f of (r.filters || [])) {
          if (f.type === 'URLRewrite') {
            if (f.urlRewrite?.hostname) return false;
            if (f.urlRewrite?.path && f.urlRewrite.path.type !== 'ReplacePrefixMatch') return false;
          } else if (f.type === 'RequestHeaderModifier') {
            if (f.requestHeaderModifier?.add || f.requestHeaderModifier?.remove) return false;
          } else { return false; }
        }
      }
      return true;
    },
    openCreate() {
      if (!this.ctx.writeEnabled) return;
      const form = this.blankForm();
      this.editor = { open: true, mode: 'create', tab: 'form', formAvailable: true,
        title: 'New route', form, raw: null, yaml: '', busy: false, error: '', result: null };
      this.formServices = { ns: '', items: [], loading: false };
      this.loadFormNamespaces();
      this.loadFormGateways();
      this.ensureFormServices();
      this.renderIcons();
    },
    securityPolicyTemplate() {
      const ns = this.namespace !== 'all' ? this.namespace : 'default';
      return [
        'apiVersion: gateway.envoyproxy.io/v1alpha1',
        'kind: SecurityPolicy',
        'metadata:',
        '  name: my-policy',
        '  namespace: ' + ns,
        'spec:',
        '  targetRefs:',
        '    - group: gateway.networking.k8s.io',
        '      kind: HTTPRoute',
        '      name: my-route',
        '  # one of: basicAuth / jwt / oidc / cors / apiKeyAuth / extAuth / authorization',
        '  cors:',
        '    allowOrigins:',
        '      - "https://example.com"',
        '    allowMethods: ["GET", "POST"]',
        '',
      ].join('\n');
    },
    openCreateYaml(title, template) {
      if (!this.ctx.writeEnabled) return;
      this.editor = { open: true, mode: 'create', tab: 'yaml', formAvailable: false,
        title, form: this.blankForm(), raw: null, yaml: template, busy: false, error: '', result: null };
      this.renderIcons();
      this.$nextTick(() => this.$refs.editorArea && this.$refs.editorArea.focus());
    },
    openEdit() {
      const raw = this.drawer.raw;
      const canForm = this.formRepresentable(raw);
      this.editor = { open: true, mode: 'edit', tab: canForm ? 'form' : 'yaml', formAvailable: canForm,
        title: 'Edit ' + this.drawer.kind + ' · ' + this.drawer.name,
        form: canForm ? this.objToForm(raw) : this.blankForm(), raw,
        yaml: this.drawer.yaml || '', busy: false, error: '', result: null };
      this.formServices = { ns: '', items: [], loading: false };
      this.loadFormNamespaces();
      this.loadFormGateways();
      if (canForm) this.ensureFormServices();
      this.renderIcons();
      if (!canForm) this.$nextTick(() => this.$refs.editorArea && this.$refs.editorArea.focus());
    },
    closeEditor() { this.editor.open = false; },
    async applyEditor() {
      if (this.editor.tab === 'form') this.editor.yaml = this.formToYaml();
      this.editor.busy = true; this.editor.error = ''; this.editor.result = null;
      try {
        const r = await fetch('/api/apply', { method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ yaml: this.editor.yaml }) });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.detail || r.statusText);
        this.editor.result = data.results || [];
        const verbs = this.editor.result.map(x => `${x.action} ${x.kind}/${x.name}`).join(', ');
        this.notify('✓ ' + (verbs || 'applied'));
        this.editor.open = false;
        await this.reload(true);
      } catch (e) {
        this.editor.error = String(e.message || e);
      } finally { this.editor.busy = false; this.renderIcons(); }
    },
    deleteObj(o) {
      // open the in-app confirmation dialog (no native confirm())
      if (!this.ctx.writeEnabled || !o) return;
      this.confirm = { open: true, target: o, busy: false };
      this.renderIcons();
    },
    closeConfirm() { if (!this.confirm.busy) this.confirm.open = false; },
    async confirmDelete() {
      const o = this.confirm.target;
      if (!o) return;
      const label = o.kind + ' ' + (o.namespace ? o.namespace + '/' : '') + o.name;
      this.confirm.busy = true;
      try {
        const q = o.namespace ? `&namespace=${encodeURIComponent(o.namespace)}` : '';
        const r = await fetch(`/api/object?kind=${encodeURIComponent(o.kind)}&name=${encodeURIComponent(o.name)}${q}`,
          { method: 'DELETE' });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.detail || r.statusText);
        this.confirm.open = false;
        this.notify('🗑 Deleted ' + label);
        this.closeDrawer();
        await this.reload(true);
      } catch (e) {
        this.notify('Delete failed: ' + (e.message || e));
      } finally { this.confirm.busy = false; this.renderIcons(); }
    },

    // ---- topology ----
    graphColumns() {
      const order = [
        { type: 'gatewayclass', title: 'Gateway Classes' },
        { type: 'gateway', title: 'Gateways' },
        { type: 'route', title: 'Routes' },
        { type: 'backend', title: 'Backends' },
      ];
      return order.map(c => ({ ...c, nodes: (this.graph.nodes || []).filter(n => n.type === c.type) }));
    },
    nodeIcon(t) { return { gatewayclass: 'box', gateway: 'door-open', route: 'route', backend: 'server' }[t] || 'circle'; },
    cssId(id) { return id.replace(/[^a-zA-Z0-9_-]/g, '_'); },

    hover(id) {
      this.hoverId = id;
      const rel = new Set([id]);
      const edges = this.graph.edges || [];
      // walk both directions a couple of hops to highlight the path
      let frontier = [id];
      for (let hop = 0; hop < 4; hop++) {
        const next = [];
        for (const e of edges) {
          if (frontier.includes(e.source) && !rel.has(e.target)) { rel.add(e.target); next.push(e.target); }
          if (frontier.includes(e.target) && !rel.has(e.source)) { rel.add(e.source); next.push(e.source); }
        }
        if (!next.length) break; frontier = next;
      }
      this.related = rel;
      this.highlightEdges();
    },
    unhover() { this.hoverId = null; this.related = new Set(); this.highlightEdges(); },

    drawEdges() {
      const svg = this.$refs.edges, wrap = this.$refs.graphWrap;
      if (!svg || !wrap) return;
      const cols = wrap.querySelector('.graph-cols');
      const W = cols.scrollWidth, H = cols.scrollHeight;
      svg.setAttribute('width', W); svg.setAttribute('height', H);
      svg.style.width = W + 'px'; svg.style.height = H + 'px';
      const wrapRect = wrap.getBoundingClientRect();
      const pt = (el, side) => {
        const r = el.getBoundingClientRect();
        return {
          x: r.left - wrapRect.left + wrap.scrollLeft + (side === 'r' ? r.width : 0),
          y: r.top - wrapRect.top + wrap.scrollTop + r.height / 2,
        };
      };
      let paths = '';
      for (const e of (this.graph.edges || [])) {
        const s = document.getElementById('node-' + this.cssId(e.source));
        const t = document.getElementById('node-' + this.cssId(e.target));
        if (!s || !t) continue;
        const a = pt(s, 'r'), b = pt(t, 'l');
        const mx = (a.x + b.x) / 2;
        paths += `<path id="edge-${this.cssId(e.source)}__${this.cssId(e.target)}" d="M${a.x},${a.y} C${mx},${a.y} ${mx},${b.y} ${b.x},${b.y}" fill="none" stroke="var(--border-strong)" stroke-width="1.5"/>`;
      }
      svg.innerHTML = paths;
    },
    highlightEdges() {
      const svg = this.$refs.edges; if (!svg) return;
      for (const p of svg.querySelectorAll('path')) {
        const [s, t] = p.id.replace('edge-', '').split('__');
        const on = this.hoverId && this.related.size;
        const active = on && this.isRelatedEdge(s, t);
        p.setAttribute('stroke', active ? 'var(--brand)' : 'var(--border-strong)');
        p.setAttribute('stroke-width', active ? '2.5' : '1.5');
        p.setAttribute('opacity', on ? (active ? '1' : '0.15') : '1');
      }
    },
    isRelatedEdge(sCss, tCss) {
      // both endpoints highlighted => edge is on the path
      let inS = false, inT = false;
      for (const id of this.related) { const c = this.cssId(id); if (c === sCss) inS = true; if (c === tCss) inT = true; }
      return inS && inT;
    },

    // ---- drawer close / esc / focus trap ----
    closeDrawer() {
      this.drawer.open = false;
      this.$nextTick(() => { if (this.lastFocused && this.lastFocused.focus) this.lastFocused.focus(); });
    },
    onEscape() {
      if (this.confirm.open) this.closeConfirm();
      else if (this.palette.open) this.closePalette();
      else if (this.editor.open) this.closeEditor();
      else if (this.drawer.open) this.closeDrawer();
    },
    trapTab(e) {
      const root = e.currentTarget;
      const f = root.querySelectorAll('a[href],button:not([disabled]),input,select,textarea,[tabindex]:not([tabindex="-1"])');
      if (!f.length) return;
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    },

    // ---- searchable namespace picker ----
    toggleNsPicker() {
      this.nsPicker.open = !this.nsPicker.open;
      if (this.nsPicker.open) {
        this.nsPicker.q = ''; this.nsPicker.sel = 0;
        this.$nextTick(() => { this.$refs.nsInput && this.$refs.nsInput.focus(); this.renderIcons(); });
      }
    },
    nsResults() {
      const q = this.nsPicker.q.trim().toLowerCase();
      return q ? this.namespaces.filter(n => n.toLowerCase().includes(q)) : this.namespaces;
    },
    nsMove(d) {
      const n = this.nsResults().length + 1; // +1 for "All namespaces"
      this.nsPicker.sel = (this.nsPicker.sel + d + n) % n;
      this.$nextTick(() => { const el = document.querySelector('.ns-opt.active'); el && el.scrollIntoView({ block: 'nearest' }); });
    },
    nsEnter() {
      if (this.nsPicker.sel === 0) return this.nsSelect('all');
      const ns = this.nsResults()[this.nsPicker.sel - 1];
      if (ns) this.nsSelect(ns);
    },
    nsSelect(ns) {
      this.namespace = ns; this.nsPicker.open = false; this.renderIcons(); this.reload();
    },

    // ---- command palette ----
    async openPalette() {
      this.lastFocused = document.activeElement;
      this.palette.open = true; this.palette.q = ''; this.palette.sel = 0;
      this.$nextTick(() => { this.$refs.paletteInput && this.$refs.paletteInput.focus(); this.renderIcons(); });
      try { this.palette.items = (await this.api('/api/index')).items || []; this.renderIcons(); } catch (e) {}
    },
    closePalette() {
      this.palette.open = false;
      this.$nextTick(() => { if (this.lastFocused && this.lastFocused.focus) this.lastFocused.focus(); });
    },
    paletteResults() {
      const q = this.palette.q.trim().toLowerCase();
      let items = this.palette.items;
      if (q) {
        const terms = q.split(/\s+/);
        items = items.filter(r => {
          const hay = (r.name + ' ' + r.kind + ' ' + (r.namespace || '')).toLowerCase();
          return terms.every(t => hay.includes(t));
        });
      }
      return items.slice(0, 30);
    },
    paletteMove(d) {
      const n = this.paletteResults().length; if (!n) return;
      this.palette.sel = (this.palette.sel + d + n) % n;
      this.$nextTick(() => { const el = document.querySelector('.palette-item.sel'); el && el.scrollIntoView({ block: 'nearest' }); });
    },
    paletteEnter() { const r = this.paletteResults()[this.palette.sel]; if (r) this.paletteOpen(r); },
    paletteOpen(r) { this.closePalette(); this.openObj(r); },
    kindIcon(kind) {
      if (kind === 'GatewayClass') return 'box';
      if (kind === 'Gateway') return 'door-open';
      if (kind.endsWith('Route')) return 'route';
      if (kind.endsWith('Policy')) return 'shield-check';
      return 'circle';
    },

    // ---- clipboard + time ----
    copy(text, label) {
      const done = () => this.notify((label || 'Copied') + ' ✓');
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done).catch(() => this.notify('Copy failed'));
      } else {
        const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta);
        ta.select(); try { document.execCommand('copy'); done(); } catch (e) { this.notify('Copy failed'); }
        document.body.removeChild(ta);
      }
    },
    kubectlCmd() {
      const d = this.drawer;
      const ns = d.namespace ? ` -n ${d.namespace}` : '';
      return `kubectl${ns} get ${d.kind.toLowerCase()} ${d.name} -o yaml`;
    },
    timeAgo(iso) {
      if (!iso) return '';
      const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
      const u = [['y', 31536000], ['mo', 2592000], ['d', 86400], ['h', 3600], ['m', 60]];
      for (const [label, secs] of u) { if (s >= secs) return Math.floor(s / secs) + label + ' ago'; }
      return 'just now';
    },

    notify(msg) { this.toast = msg; clearTimeout(this._toast); this._toast = setTimeout(() => this.toast = '', 2600); },
  };
}

// persist namespace selection
document.addEventListener('alpine:init', () => {});
window.addEventListener('beforeunload', () => {});
