(function () {
  const storageKey = 'zabbix-view-mode';
  let mode = null;
  try {
    mode = localStorage.getItem(storageKey);
  } catch (_) {
    // The automatic layout still works when browser storage is unavailable.
  }
  if (mode !== 'mobile' && mode !== 'desktop') mode = null;

  if (mode) document.documentElement.dataset.viewMode = mode;
  if (mode === 'desktop') {
    document.querySelector('meta[name="viewport"]').content = 'width=1200, initial-scale=1';
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-view-mode]').forEach(function (button) {
      if (button.tagName !== 'BUTTON') return;
      button.setAttribute('aria-pressed', String(button.dataset.viewMode === mode));
      button.addEventListener('click', function () {
        try {
          localStorage.setItem(storageKey, button.dataset.viewMode);
        } catch (_) {
          // Apply the selected layout for this page even without persistence.
          mode = button.dataset.viewMode;
          document.querySelector('meta[name="viewport"]').content = mode === 'desktop'
            ? 'width=1200, initial-scale=1' : 'width=device-width, initial-scale=1';
          applyMode(button.dataset.viewMode);
          document.querySelectorAll('.view-mode-options button').forEach(function (option) {
            option.setAttribute('aria-pressed', String(option.dataset.viewMode === mode));
          });
          return;
        }
        location.reload();
      });
    });
  });

  function compactRule(rule) {
    // The device cards switch to one column at 1050px; their compact details switch at 720px.
    return rule.type === CSSRule.MEDIA_RULE && /max-width\s*:\s*(?:600|720|1050)px/i.test(rule.conditionText);
  }

  const compactRules = [];
  let compactRulesCaptured = false;
  function applyMode(selectedMode) {
    document.documentElement.dataset.viewMode = selectedMode;
    document.getElementById('forced-mobile-rules')?.remove();
    for (const sheet of document.styleSheets) {
      let rules;
      try {
        rules = sheet.cssRules;
      } catch (_) {
        continue;
      }
      if (!compactRulesCaptured) {
        for (const rule of rules) {
          if (compactRule(rule)) compactRules.push(Array.from(rule.cssRules, nested => nested.cssText).join('\n'));
        }
      }
      if (selectedMode === 'desktop') {
        for (let index = rules.length - 1; index >= 0; index--) {
          if (compactRule(rules[index])) sheet.deleteRule(index);
        }
      }
    }
    compactRulesCaptured = true;
    if (selectedMode === 'mobile') {
      const style = document.createElement('style');
      style.id = 'forced-mobile-rules';
      style.textContent = compactRules.join('\n');
      document.head.appendChild(style);
    }
  }

  if (mode) window.addEventListener('load', function () { applyMode(mode); });
})();
