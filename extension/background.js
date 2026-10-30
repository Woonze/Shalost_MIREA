chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (message.target === 'offscreen') return;
  if (!['start', 'stop', 'presence'].includes(message.type)) return;
  (async () => {
    if (message.type === 'presence') {
      const tab = await chrome.tabs.get(message.tabId);
      if (!tab.url || new URL(tab.url).hostname !== 'my.mts-link.ru') return {status: 'unsupported'};
      const results = await chrome.scripting.executeScript({target: {tabId: message.tabId, allFrames: true}, func: () => {
        const normalize = text => (text || '').replace(/\s+/g, ' ').trim().toLowerCase();
        const buttons = [...document.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]')];
        for (const button of buttons) {
          if (normalize(button.innerText || button.value || button.getAttribute('aria-label')) !== 'подтверждаю') continue;
          if (!button.getClientRects().length || button.disabled || button.getAttribute('aria-disabled') === 'true') continue;
          let container = button;
          for (let i = 0; i < 8 && container; i++, container = container.parentElement) {
            const text = normalize(container.innerText);
            if (text.length <= 500 && text.includes('контроль присутствия') && text.includes('подтвердите, что вы участвуете')) {
              button.click();
              return 'clicked';
            }
          }
        }
        return 'absent';
      }});
      return {status: results.some(result => result.result === 'clicked') ? 'clicked' : 'absent'};
    }
    if (message.type === 'stop') {
      if (await chrome.offscreen.hasDocument()) await chrome.runtime.sendMessage({target: 'offscreen', type: 'stop'});
      return {};
    }
    const connection = await fetch('http://127.0.0.1:47881/check', {method: 'POST', headers: {'X-Shalost-Key': message.key}});
    if (!connection.ok) throw new Error('Неверный ключ приложения');
    if (!(await chrome.offscreen.hasDocument())) {
      await chrome.offscreen.createDocument({url: 'offscreen.html', reasons: ['USER_MEDIA'], justification: 'Поиск QR на подключённой вкладке'});
    }
    const streamId = await chrome.tabCapture.getMediaStreamId({targetTabId: message.tabId});
    return await chrome.runtime.sendMessage({target: 'offscreen', type: 'start', streamId, key: message.key, tabId: message.tabId});
  })().then(respond).catch(error => respond({error: error.message}));
  return true;
});
