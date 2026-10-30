// ダウンロードボタンが押された時の処理
document.getElementById('dl-btn').addEventListener('click', async () => {
  const url = document.getElementById('video-url').value;
  const btn = document.getElementById('dl-btn');
  const status = document.getElementById('status');
  const httpCodeDisplay = document.getElementById('http-status-code');

  if (!url) return alert('URLを入力してください');

  btn.disabled = true;
  httpCodeDisplay.innerText = "通信中...";
  httpCodeDisplay.style.color = "#f1c40f";
  status.innerText = "🚀 Vercelの安全な裏窓口を経由して起動を要請中...";

  try {
    // 自分のVercel内に作った安全な裏ルート（/api/trigger）へPOST通信を送信
    const response = await fetch('/api/trigger', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ video_url: url })
    });

    const data = await response.json();
    
    // GitHub Actionsから返ってきた生の応答コード（204など）を表示窓に点灯させる
    httpCodeDisplay.innerText = data.status || response.status;

    if (data.status === 204) {
      // 204は大成功なので緑色にする
      httpCodeDisplay.style.color = "#2ecc71";
      status.innerHTML = "🎉 大成功（HTTP 204）！GitHub Actionsサーバーが完全に起動しました。<br>動画をダウンロードしてHugging Faceの50GBストレージへ直書きしています！そのままで大丈夫です。";
    } else {
      httpCodeDisplay.style.color = "#e74c3c";
      status.innerText = `❌ 起動要請が拒否されました。エラーコード: ${data.status}（トークンやリポジトリ設定を確認してください）`;
      btn.disabled = false;
    }
  } catch (error) {
    httpCodeDisplay.innerText = "ERR";
    httpCodeDisplay.style.color = "#e74c3c";
    status.innerText = "❌ 通信エラーが発生しました。Vercelの金庫（Environment Variables）の設定を確認してください。";
    btn.disabled = false;
  }
});
