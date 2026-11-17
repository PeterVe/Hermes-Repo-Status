// The web dashboard has no Repo Status page (yet): the UI lives in the Desktop
// half (desktop/plugin.js). This placeholder satisfies the hidden-tab contract.
(function () {
  if (window.__HERMES_PLUGINS__) {
    window.__HERMES_PLUGINS__.register('repo-status', function () { return null })
  }
})()
