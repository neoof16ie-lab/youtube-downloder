export default async function handler(req, res) {
  // ブラウザのCORSブロックを完全に解除するためのヘッダー設定
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'POST, GET, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

  // 事前通信（OPTIONS）は200で即復帰
  if (req.method === 'OPTIONS') {
    return res.status(200).end();
  }

  // POSTメソッド以外は弾く
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'POSTメソッドのみ受け付けます' });
  }

  const { video_url } = req.body;
  
  // Vercelの隠し金庫（環境変数）からトークンを安全に抽出
  const TOKEN = process.env.GITHUB_USER;
  const TOKEN = process.env.GITHUB_REPO;
  const TOKEN = process.env.GITHUB_TOKEN; 
  
  try {
    // あなたが教えてくれた正しい /dispatches 宛てに、Vercelのサーバー内部から通信を安全に送信
    const response = await fetch(`https://github.com{GITHUB_USER}/${GITHUB_REPO}/dispatches`, {
      method: 'POST',
      headers: {
        'Authorization': `token ${TOKEN}`,
        'Accept': 'application/vnd.github.v3+json',
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        event_type: 'trigger-download', // yml側のトリガー名と一致
        client_payload: { video_url: video_url }
      })
    });

    // GitHubから返ってきたステータスコードをそのままフロントへ返す
  return res.status(response.status).json({ success: true });
  } catch (error) {
    return res.status(500).json({ error: 'GitHub Actionsへの接続に失敗しました' });
  }
}
