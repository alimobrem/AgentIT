/* AgentIT portal -- Events notification drawer (Alpine component).
   Extracted from templates/base.html's embedded <script> block
   (2026-07-20 base.html split) -- content is otherwise unmodified. */

// Events notification drawer -- loads real events from /api/events
// (same AssessmentStore.list_events the Events page uses). Never mocked.
// Focus pattern mirrors confirmModal / commandPalette: move focus into
// the dialog on open, restore the trigger on close, trap Tab while open.
function eventsDrawer() {
  return {
    navOpen: false,
    open: false,
    loading: false,
    error: '',
    events: [],
    badgeCount: 0,
    _lastSeenKey: 'agentit.events.lastSeenAt',
    _returnFocus: null,
    _trapHandler: null,
    init() {
      // Unread/critical badge from real events — not mocked.
      this.refreshBadge();
    },
    // hx-boost destroyTree tears down this component without close() —
    // drop any document-level Tab/Esc trap so it cannot outlive the DOM.
    destroy() {
      this._removeTrap();
    },
    async refreshBadge() {
      try {
        const res = await fetch('/api/events?limit=50');
        if (!res.ok) return;
        const rows = await res.json();
        this.badgeCount = this._computeBadge(rows);
      } catch (e) {
        // Leave badge at 0 if the feed is unreachable.
      }
    },
    _isBadgeSeverity(e) {
      // Only critical/high (actionable) ever badge — info/noise must not
      // permanently badge after the drawer is opened / last-seen advances.
      return e && (e.severity === 'critical' || e.severity === 'high');
    },
    _computeBadge(rows) {
      if (!Array.isArray(rows) || !rows.length) return 0;
      let lastSeen = '';
      try { lastSeen = localStorage.getItem(this._lastSeenKey) || ''; } catch (e) { lastSeen = ''; }
      const self = this;
      return rows.filter(function(e) {
        if (!self._isBadgeSeverity(e)) return false;
        // First visit (no last-seen): surface all critical/high in the window.
        // After open: only newer critical/high count as unread.
        if (!lastSeen) return true;
        return e.timestamp && e.timestamp > lastSeen;
      }).length;
    },
    _eventHref(e) {
      // Prefer app-scoped action surfaces when we have an assessment id
      // (enriched by /api/events); else that app's PRs on Ledger; else
      // correlation filter on Events.
      if (e && e.assessment_id) {
        return '/assessments/' + e.assessment_id + '?tab=ledger';
      }
      if (e && e.target_app) {
        return '/ledger?app=' + encodeURIComponent(e.target_app);
      }
      if (e && e.correlation_id) {
        return '/events?correlation_id=' + encodeURIComponent(e.correlation_id);
      }
      return '/events';
    },
    _badgeClass(e) {
      // Always return a severity color class — unknown/empty → info. Events'
      // own severity vocabulary (see every store.log_event() call site) is
      // critical/error/warning/info -- "high"/"medium"/"low" are a
      // finding's severity, never an event's, but stayed here from when
      // this helper was first written against that assumption.
      var s = (e && e.severity) || 'info';
      if (s === 'critical') return 'badge badge-critical';
      if (s === 'error') return 'badge badge-danger';
      if (s === 'warning') return 'badge badge-warning';
      return 'badge badge-info';
    },
    _markSeen(rows) {
      var maxTs = '';
      (rows || []).forEach(function(e) {
        if (e.timestamp && e.timestamp > maxTs) maxTs = e.timestamp;
      });
      if (!maxTs) maxTs = new Date().toISOString();
      try { localStorage.setItem(this._lastSeenKey, maxTs); } catch (e) { /* private mode */ }
      this.badgeCount = 0;
    },
    async openDrawer() {
      if (this.open) return;
      this._returnFocus = document.activeElement;
      this.open = true;
      this.loading = true;
      this.error = '';
      this.events = [];
      this.$nextTick(() => {
        this._installTrap();
        if (this.$refs.closeBtn) this.$refs.closeBtn.focus();
      });
      try {
        const res = await fetch('/api/events?limit=20');
        if (!res.ok) {
          this.error = 'Could not load events.';
        } else {
          this.events = await res.json();
          this._markSeen(this.events);
        }
      } catch (e) {
        this.error = 'Could not load events.';
      }
      this.loading = false;
    },
    close() {
      if (!this.open) return;
      this.open = false;
      this._removeTrap();
      const target = (this._returnFocus && typeof this._returnFocus.focus === 'function')
        ? this._returnFocus
        : this.$refs.bellBtn;
      this._returnFocus = null;
      this.$nextTick(() => { if (target) target.focus(); });
    },
    _focusable() {
      const root = this.$refs.drawerPanel;
      if (!root) return [];
      // getClientRects (not offsetParent): position:fixed nodes often have
      // a null offsetParent even when visible.
      return Array.from(root.querySelectorAll(
        'a[href], button:not([disabled]), textarea, input, select, [tabindex]:not([tabindex="-1"])'
      )).filter(function(el) {
        return el.getClientRects().length > 0;
      });
    },
    _installTrap() {
      this._removeTrap();
      const self = this;
      this._trapHandler = function(e) {
        if (!self.open) return;
        // Esc here mirrors @keydown.escape.window so close works even when
        // focus is outside the Alpine scope or a nested menu also listens.
        if (e.key === 'Escape') {
          self.close();
          return;
        }
        if (e.key !== 'Tab') return;
        const nodes = self._focusable();
        if (!nodes.length) return;
        const first = nodes[0];
        const last = nodes[nodes.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      };
      document.addEventListener('keydown', this._trapHandler);
    },
    _removeTrap() {
      if (this._trapHandler) {
        document.removeEventListener('keydown', this._trapHandler);
        this._trapHandler = null;
      }
    },
  };
}
