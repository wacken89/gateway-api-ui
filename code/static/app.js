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
    drawer: { open: false, tab: 'summary', kind: '', name: '', namespace: '', yaml: '', raw: null, loading: false, related: [], relatedLoading: false },
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
          this.routes = (await this.api('/api/routes' + q)).items;
          this.counts.routes = this.routes.length;
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
    async openObj(o) {
      if (!o || !o.kind) return;
      if (!this.DRAWER_KINDS.includes(o.kind)) {
        this.notify(o.kind + ' ' + (o.namespace ? o.namespace + '/' : '') + o.name + ' — not a Gateway API object');
        return;
      }
      if (!this.drawer.open) this.lastFocused = document.activeElement;
      const keepTab = this.drawer.open ? this.drawer.tab : 'summary';
      this.drawer = { open: true, tab: keepTab, kind: o.kind, name: o.name, namespace: o.namespace || '', yaml: '', raw: null, loading: true, related: [], relatedLoading: true };
      this.renderIcons();
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
    openNode(n) {
      if (!n.ref || !n.ref.kind || n.ref.kind === 'Service') {
        this.notify(n.ref?.kind === 'Service' ? 'Backend Service: ' + n.label : n.label);
        return;
      }
      this.openObj(n.ref);
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
      if (this.palette.open) this.closePalette();
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
