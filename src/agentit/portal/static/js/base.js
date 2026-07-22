/* AgentIT portal -- core shell script: CSRF header injection for every
   htmx request, button/loading-bar feedback, htmx error toasts, the
   Alpine re-init-on-htmx-boost fix, the shared confirm modal, the toast
   notification manager, and relative-timestamp formatting.
   Extracted from templates/base.html's embedded <script> block
   (2026-07-20 base.html split) -- content is otherwise unmodified. See
   static/js/events-drawer.js and static/js/command-palette.js for the
   two Alpine components that were split out separately. */

htmx.config.timeout = 120000;
// CSRF: every state-changing route needs the double-submit-cookie token
// (see csrf.py) echoed back as the X-CSRF-Token header. Since <body> has
// hx-boost="true", htmx intercepts every plain <form method="post"> in
// every template -- so attaching it here on htmx:configRequest covers all
// of them without editing each template's <form> tag individually.
function getCsrfCookie() {
  var match = document.cookie.match(/(?:^|; )csrf_token=([^;]*)/);
  return match ? decodeURIComponent(match[1]) : '';
}
document.body.addEventListener('htmx:configRequest', function(e) {
  e.detail.headers['X-CSRF-Token'] = getCsrfCookie();
});
document.body.addEventListener('htmx:beforeRequest', function(e) {
  var btn = e.detail.elt.querySelector('button[type="submit"]');
  if (btn) btn.classList.add('btn-loading');
});
document.body.addEventListener('htmx:afterRequest', function(e) {
  var btn = e.detail.elt.querySelector('button[type="submit"]');
  if (btn) btn.classList.remove('btn-loading');
});
// Show spinner on synchronous form submits
document.addEventListener('submit', function(e) {
  var form = e.target;
  if (form.hasAttribute('hx-post') || form.hasAttribute('hx-get')) return;
  var btn = form.querySelector('button[type="submit"]') || form.querySelector('.btn');
  if (!btn) return;
  btn.disabled = true;
  btn.classList.add('btn-loading');
  btn.setAttribute('aria-busy', 'true');
});
// Instant feedback on ALL clickable buttons (not just form submits)
document.addEventListener('click', function(e) {
  var btn = e.target.closest('.btn, button[type="submit"]');
  if (!btn || btn.disabled) return;
  btn.classList.add('btn-clicked');
  setTimeout(function() { btn.classList.remove('btn-clicked'); }, 300);
});
// Show loading bar on nav clicks and form submits
var loadingBar = document.getElementById('nav-loading');
function showLoading() {
  loadingBar.style.width = '0';
  loadingBar.classList.remove('active');
  void loadingBar.offsetWidth;
  loadingBar.classList.add('active');
  document.body.style.cursor = 'progress';
}
document.addEventListener('submit', function() { showLoading(); });
// Also trigger on htmx boosted requests -- covers every nav link/form
// click already (body has hx-boost="true"), so no separate per-link
// listener is needed (and one bound only to first-page-load anchors
// would be discarded by htmx-boosted navigation anyway).
document.body.addEventListener('htmx:beforeRequest', function() { showLoading(); });
// Loading bar completion
function completeLoading() {
  var bar = document.querySelector('.nav-loading-bar');
  if (bar && bar.classList.contains('active')) {
    bar.style.transition = 'width 0.2s';
    bar.style.width = '100%';
    setTimeout(function() {
      bar.classList.remove('active');
      bar.style.width = '0';
      bar.style.transition = '';
    }, 300);
  }
  document.body.style.cursor = '';
}
document.body.addEventListener('htmx:afterSettle', completeLoading);
// Re-initialize Alpine on htmx-swapped content so x-data, @click, $dispatch work
document.body.addEventListener('htmx:afterSettle', function(e) {
  if (!window.Alpine || !e.detail) return;
  if (e.detail.boosted) {
    // Boosted nav links/forms (hx-boost="true" on <body>) replace <body>'s
    // children wholesale on every click. A plain initTree() here isn't
    // enough: Alpine's own MutationObserver-based auto-init races with
    // htmx's settle phase (which pauses briefly for CSS transitions), so
    // directives like x-show can end up bound against an intermediate,
    // not-yet-settled DOM state -- symptom: the Events drawer panel stays
    // visible after any boosted nav click even though its `open` state is
    // false, because its x-show binding never actually attached. destroyTree()
    // first tears down whatever Alpine's own observer already
    // (mis)initialized, so initTree() then binds cleanly against the final,
    // settled DOM -- the fix Alpine's own maintainers recommend for this
    // exact htmx-boost interaction (alpinejs/alpine discussion #4485).
    Alpine.destroyTree(document.body);
    Alpine.initTree(document.body);
    // alpine:initialized only fires once per full page load. Boosted POST
    // redirects (Register for GitOps, Deliver, Delete, ...) land ?error=
    // /?success= on the new URL with a swapped body, and without this the
    // flash toasts never appear -- the click looks like a no-op.
    showUrlParamToasts();
  } else if (e.detail.target && e.detail.target.querySelector('[x-data]')) {
    Alpine.initTree(e.detail.target);
  }
});
document.body.addEventListener('htmx:responseError', function(e) {
  completeLoading();
  var tm = document.getElementById('toasts');
  if (tm && Alpine.$data(tm)) {
    var status = e.detail.xhr ? e.detail.xhr.status : 'unknown';
    Alpine.$data(tm).show('Request failed (HTTP ' + status + '). Please try again.', 'error');
  }
});
document.body.addEventListener('htmx:timeout', function() {
  completeLoading();
  var tm = document.getElementById('toasts');
  if (tm && Alpine.$data(tm)) {
    Alpine.$data(tm).show('Request timed out. Please try again.', 'warning');
  }
});
document.body.addEventListener('htmx:sendError', function() {
  // Genuine network-level failure (request never reached the server) --
  // distinct from htmx:responseError (a real HTTP error response). Any
  // optimistic UI that predicted success (e.g. the Suppress action's
  // instant hide) reconciles via its own local @htmx:send-error listener;
  // this just makes sure the failure is never silent.
  completeLoading();
  var tm = document.getElementById('toasts');
  if (tm && Alpine.$data(tm)) {
    Alpine.$data(tm).show('Network error — request could not be sent. Please try again.', 'error');
  }
});
document.body.addEventListener('htmx:beforeSwap', function(e) {
  if (e.detail.xhr.status >= 400 && e.detail.xhr.status < 500) {
    e.detail.shouldSwap = false;
    completeLoading();
    var tm = document.getElementById('toasts');
    if (tm && Alpine.$data(tm)) {
      Alpine.$data(tm).show('Invalid request — please check your input.', 'error');
    }
  }
});
window.addEventListener('pageshow', completeLoading);
// Dead-button detector: flag @click handlers outside Alpine x-data scope
document.addEventListener('alpine:init', function() {
  setTimeout(function() {
    document.querySelectorAll('[\\@click], [x-on\\:click]').forEach(function(el) {
      if (!el.closest('[x-data]')) {
        console.error('Dead button: @click outside x-data scope', el);
      }
    });
  }, 100);
});
// Confirm modal component
function confirmModal() {
  return {
    open: false,
    title: '',
    message: '',
    confirmText: 'Confirm',
    dangerClass: 'btn-green',
    // Type-to-confirm (opts.typeToConfirm): set to the exact string the user
    // must type before Confirm enables -- reserved for wide-blast-radius
    // actions only (see the template comment above). Empty string/falsy
    // means "ordinary confirm", the default for every other caller.
    typedConfirmTarget: '',
    typedValue: '',
    _form: null,
    show(title, message, form, opts) {
      opts = opts || {};
      this.title = title;
      this.message = message;
      this.confirmText = opts.confirmText || 'Confirm';
      this.dangerClass = opts.danger ? 'btn-danger' : 'btn-green';
      this.typedConfirmTarget = opts.typeToConfirm || '';
      this.typedValue = '';
      this._form = form;
      this.open = true;
      // Cancel gets default focus on every confirm -- including
      // destructive/type-to-confirm ones -- so a reflexive Enter keypress
      // never fires the guarded action (Material 3 / Apple HIG / Carbon /
      // Polaris / GOV.UK's converging rule). $nextTick waits for the modal's
      // just-set `open=true` to actually paint before focusing it.
      this.$nextTick(() => { if (this.$refs.cancelBtn) this.$refs.cancelBtn.focus(); });
    },
    cancel() { this.open = false; this._form = null; this.typedConfirmTarget = ''; this.typedValue = ''; },
    proceed() {
      if (this.typedConfirmTarget && this.typedValue !== this.typedConfirmTarget) return;
      this.open = false;
      // requestSubmit() (unlike submit()) fires a real 'submit' event, so htmx's
      // submit interception still kicks in for hx-post/hx-get forms and does a
      // smooth AJAX swap instead of falling through to a full page reload.
      if (this._form) this._form.requestSubmit();
    }
  };
}

// Footer action-feedback strip (fixed chrome). Idle until showToast /
// HTMX error paths push a message; auto-clears back to Ready.
function actionFeedback() {
  return {
    message: 'Ready',
    type: 'idle',
    _timer: null,
    show(message, type, duration) {
      type = type || 'info';
      duration = (duration === undefined) ? 8000 : duration;
      this.message = message;
      this.type = type;
      if (this._timer) clearTimeout(this._timer);
      var self = this;
      if (duration > 0) {
        this._timer = setTimeout(function() {
          self.message = 'Ready';
          self.type = 'idle';
          self._timer = null;
        }, duration);
      }
    }
  };
}
function setActionFeedback(message, type, duration) {
  var el = document.getElementById('action-feedback');
  if (el && window.Alpine && Alpine.$data(el)) {
    Alpine.$data(el).show(message, type, duration);
  }
}
// Toast notification manager
function toastManager() {
  return {
    toasts: [],
    _id: 0,
    show(message, type, duration) {
      type = type || 'info';
      duration = (duration === undefined) ? 5000 : duration;
      var id = ++this._id;
      this.toasts.push({ id: id, message: message, type: type, visible: true });
      // Mirror into the footer status strip so action outcomes stay visible
      // in fixed chrome even after the floating toast dismisses.
      setActionFeedback(message, type, duration > 0 ? Math.max(duration, 8000) : 0);
      var self = this;
      if (duration > 0) setTimeout(function() { self.dismiss(id); }, duration);
    },
    dismiss(id) {
      var t = this.toasts.find(function(t) { return t.id === id; });
      if (t) t.visible = false;
      var self = this;
      setTimeout(function() { self.toasts = self.toasts.filter(function(t) { return t.id !== id; }); }, 300);
    }
  };
}
// Small reusable wrapper so a plain @click can fire a toast without every
// call site repeating the getElementById + Alpine.$data boilerplate --
// e.g. a long-running action's "this can take a few minutes" caveat,
// which belongs in a dismissible notification, not crammed into the
// triggering button's own label/loading text.
function showToast(message, type, duration) {
  var tm = document.getElementById('toasts');
  if (tm && Alpine.$data(tm)) Alpine.$data(tm).show(message, type, duration);
  else setActionFeedback(message, type, duration);
}
// Auto-show toasts from URL params. Must wait for 'alpine:initialized' (fires
// once ALL components on the page have been initialized), not 'alpine:init'
// (fires *before* any x-data component is initialized -- that event exists
// for registering custom directives/stores via Alpine.data()/Alpine.store(),
// not for reading already-initialized component data via Alpine.$data()).
// Using 'alpine:init' here meant Alpine.$data(tm) could intermittently be
// undefined/not-yet-bound depending on init timing, causing
// "Alpine.$data(...).show is not a function".
// Also re-invoked from htmx:afterSettle after boosted redirects (see call
// above) because alpine:initialized does not re-fire when
// Alpine.destroyTree/initTree rebinds a swapped body.
function showUrlParamToasts() {
  var params = new URLSearchParams(window.location.search);
  var tm = document.getElementById('toasts');
  if (!tm || !window.Alpine || !Alpine.$data(tm)) return;
  var data = Alpine.$data(tm);
  if (params.get('error') && !document.querySelector('.alert-error')) data.show(params.get('error'), 'error');
  if (params.get('warning') && !document.querySelector('.alert-warn')) data.show(params.get('warning'), 'warning');
  if ((params.get('success') || params.get('pr_url')) && !document.querySelector('.alert-success')) {
    data.show(params.get('success') || 'Operation successful', 'success');
  }
  if (params.get('purged')) data.show('Purged ' + params.get('purged') + ' stale rows', 'success');
}
document.addEventListener('alpine:initialized', showUrlParamToasts);
// Relative timestamps
function relativeTime(iso) {
  var d = new Date(iso);
  // Many call sites pass a "—" placeholder (or an empty string) for agents/
  // rows that genuinely have no timestamp yet (see e.g. agents.html's
  // synthetic watcher-agent entries). new Date() on that is an Invalid Date,
  // and toLocaleDateString() on an Invalid Date returns the literal string
  // "Invalid Date" -- returning null here instead tells the caller to leave
  // whatever the server already rendered (its own "—" fallback) untouched.
  if (isNaN(d.getTime())) return null;
  var now = new Date();
  var diff = (now - d) / 1000;
  if (diff < 60) return 'just now';
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  if (diff < 604800) return Math.floor(diff / 86400) + 'd ago';
  return d.toLocaleDateString();
}
function formatTimestamps(root) {
  (root || document).querySelectorAll('[data-timestamp]').forEach(function(el) {
    var iso = el.getAttribute('data-timestamp');
    var rel = relativeTime(iso);
    if (rel === null) return;
    el.textContent = rel;
    el.title = iso;
  });
}
document.addEventListener('DOMContentLoaded', function() { formatTimestamps(document); });
// DOMContentLoaded only fires once per real page load -- it never re-fires
// for htmx-swapped content (boosted or not), so any [data-timestamp]
// element that only exists after a swap (i.e. everywhere except whatever
// page happened to load first) was permanently stuck showing the server's
// raw ISO-sliced fallback text instead of "3m ago". Re-run on every settle.
document.body.addEventListener('htmx:afterSettle', function(e) {
  formatTimestamps(e.detail && e.detail.boosted ? document : (e.detail && e.detail.target) || document);
});
