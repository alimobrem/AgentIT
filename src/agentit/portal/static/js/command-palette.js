/* AgentIT portal -- Cmd+K / Ctrl+K command palette (Alpine component).
   Extracted from templates/base.html's embedded <script> block
   (2026-07-20 base.html split) -- content is otherwise unmodified. */

// Command palette (Cmd+K / Ctrl+K) -- a genuinely reusable Alpine
// component, not a one-off: fuzzy-searches every nav destination plus
// every real app in the fleet (fetched from /api/fleet, never mocked).
// See base.html's #command-palette markup for the trigger + result list.
function commandPalette() {
  return {
    open: false,
    query: '',
    results: [],
    activeIndex: 0,
    apps: null, // null = not yet fetched; [] = fetched, fleet is empty
    navItems: [
      { label: 'Ledger', href: '/ledger', hint: 'Morning inbox — PRs waiting for your approval' },
      { label: 'Fleet', href: '/fleet', hint: 'Portfolio scoreboard — apps and scores' },
      { label: 'Health', href: '/health', hint: 'Live infrastructure telemetry' },
      { label: 'Insights', href: '/insights', hint: 'Fleet-wide aggregate analytics' },
      { label: 'Events', href: '/events', hint: 'Every system action, behind the scenes' },
      { label: 'Dead-Letter Queue', href: '/events/dlq', hint: 'Undeliverable events' },
      { label: 'Decisions', href: '/decisions', hint: 'LLM decision audit — Events owns the stream' },
      { label: 'Capabilities', href: '/capabilities', hint: 'Agent registry & catalog' },
      { label: 'Settings', href: '/settings', hint: 'Auto-mode & data retention' },
      { label: 'Schedules', href: '/schedules', hint: 'Scheduled operations' },
      { label: 'Fleet SLOs', href: '/fleet/slos', hint: 'SLOs across every app' },
    ],
    handleGlobalKeydown(e) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        this.open ? this.close() : this.openPalette();
      }
    },
    async openPalette() {
      this.open = true;
      this.query = '';
      this.activeIndex = 0;
      this.computeResults();
      this.$nextTick(() => { if (this.$refs.paletteInput) this.$refs.paletteInput.focus(); });
      if (this.apps === null) {
        try {
          const res = await fetch('/api/fleet');
          this.apps = res.ok ? await res.json() : [];
        } catch (e) {
          this.apps = [];
        }
        this.computeResults();
      }
    },
    close() { this.open = false; },
    // Simple case-insensitive fuzzy match: exact substring scores highest
    // (favoring earlier matches), otherwise an in-order subsequence match
    // (every needle char found, in order, somewhere in the haystack).
    // Returns -1 for no match at all.
    fuzzyScore(needle, haystack) {
      needle = (needle || '').toLowerCase();
      haystack = (haystack || '').toLowerCase();
      if (!needle) return 1;
      const idx = haystack.indexOf(needle);
      if (idx !== -1) return 1000 - idx;
      let ni = 0, score = 0;
      for (let hi = 0; hi < haystack.length && ni < needle.length; hi++) {
        if (haystack[hi] === needle[ni]) { ni++; score++; }
      }
      return ni === needle.length ? score : -1;
    },
    computeResults() {
      const q = this.query.trim();
      const navMatches = this.navItems
        .map((item) => ({ item, score: this.fuzzyScore(q, item.label) }))
        .filter((m) => m.score >= 0)
        .sort((a, b) => b.score - a.score)
        .map((m) => ({ type: 'nav', label: m.item.label, hint: m.item.hint, href: m.item.href }));
      const apps = this.apps || [];
      const appMatches = apps
        .map((a) => ({ a, score: this.fuzzyScore(q, a.repo_name) }))
        .filter((m) => m.score >= 0)
        .sort((a, b) => b.score - a.score)
        .slice(0, 8)
        .map((m) => ({
          type: 'app', label: m.a.repo_name,
          hint: 'Score ' + Math.round(m.a.latest_score || 0) + '/100 · ' + (m.a.criticality || ''),
          href: '/assessments/' + m.a.id,
          repoUrl: m.a.repo_url, criticality: m.a.criticality,
        }));
      this.results = navMatches.concat(appMatches);
      this.activeIndex = 0;
    },
    go(r) {
      this.close();
      window.location.href = r.href;
    },
    // Same /assess endpoint as Fleet's own Scan button -- assess_submit()
    // already defaults continue_onboard to "1" server-side (chains straight
    // into onboard) for every caller, so there's nothing case-specific to
    // set here either way.
    async reassess(r) {
      this.close();
      try {
        const body = {
          repo_url: r.repoUrl || '',
          criticality: r.criticality || 'medium',
        };
        const res = await fetch('/assess', {
          method: 'POST',
          headers: { 'X-CSRF-Token': getCsrfCookie(), 'Content-Type': 'application/x-www-form-urlencoded' },
          body: new URLSearchParams(body),
        });
        window.location.href = res.url || '/';
      } catch (e) {
        window.location.href = '/';
      }
    },
    moveDown() { this.activeIndex = Math.min(this.activeIndex + 1, this.results.length - 1); },
    moveUp() { this.activeIndex = Math.max(this.activeIndex - 1, 0); },
    selectActive() { if (this.results[this.activeIndex]) this.go(this.results[this.activeIndex]); },
  };
}
