const keyInput = document.getElementById('key');
const statusText = document.getElementById('status');
chrome.storage.session.get('key').then(({key}) => { keyInput.value = key || ''; });
document.getElementById('start').addEventListener('click', async () => {
  try {
    const key = keyInput.value.trim();
    if (!key) throw new Error('Введите ключ приложения');
    const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
    await chrome.storage.session.set({key});
    const result = await chrome.runtime.sendMessage({type: 'start', tabId: tab.id, key});
    if (result.error) throw new Error(result.error);
    statusText.textContent = 'Вкладка подключена';
  } catch (error) { statusText.textContent = `Ошибка: ${error.message}`; }
});
document.getElementById('stop').addEventListener('click', async () => {
  await chrome.runtime.sendMessage({type: 'stop'});
  statusText.textContent = 'Захват остановлен';
});
