let stream = null;
let timer = null;
let busy = false;
let presencePending = false;
const video = document.createElement('video');
video.muted = true;
const canvas = document.createElement('canvas');
function stop() {
  clearInterval(timer);
  timer = null;
  if (stream) stream.getTracks().forEach(track => track.stop());
  stream = null;
  presencePending = false;
  video.srcObject = null;
}
chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (message.target !== 'offscreen') return;
  if (message.type === 'stop') { stop(); respond({}); return; }
  if (message.type !== 'start') return;
  (async () => {
    stop();
    stream = await navigator.mediaDevices.getUserMedia({audio: false, video: {mandatory: {chromeMediaSource: 'tab', chromeMediaSourceId: message.streamId}}});
    stream.getVideoTracks()[0].addEventListener('ended', stop);
    video.srcObject = stream;
    await video.play();
    timer = setInterval(async () => {
      if (busy || !video.videoWidth || !video.videoHeight) return;
      busy = true;
      try {
        const headers = {'X-Shalost-Key': message.key};
        const mode = await fetch('http://127.0.0.1:47881/mode', {method: 'POST', headers});
        if (!mode.ok) throw new Error('Приложение недоступно');
        if ((await mode.json()).scanning) {
          const result = await chrome.runtime.sendMessage({type: 'presence', tabId: message.tabId});
          if (result.error) console.error('Контроль присутствия:', result.error);
          if (result.status === 'clicked') presencePending = true;
          else if (result.status === 'absent' && presencePending) {
            presencePending = false;
            await fetch('http://127.0.0.1:47881/presence', {method: 'POST', headers});
          }
        } else {
          presencePending = false;
          return;
        }
        canvas.width = video.videoWidth;
        canvas.height = video.videoHeight;
        canvas.getContext('2d').drawImage(video, 0, 0);
        const frame = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.9));
        await fetch('http://127.0.0.1:47881/frame', {method: 'POST', headers: {'X-Shalost-Key': message.key}, body: frame});
      } catch (error) { console.error(error); }
      finally { busy = false; }
    }, 2000);
    return {};
  })().then(respond).catch(error => respond({error: error.message}));
  return true;
});
