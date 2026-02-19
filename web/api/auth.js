/**
 * 인증 API
 * POST /api/auth
 * Body: { password: string }
 * Returns: { success: boolean }
 */
module.exports = async (req, res) => {
  // CORS
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
  if (req.method === "OPTIONS") return res.status(200).end();

  if (req.method !== "POST") {
    return res.status(405).json({ error: "POST만 허용됩니다." });
  }

  const { password } = req.body || {};
  const appPassword = process.env.APP_PASSWORD;

  if (!appPassword) {
    return res.status(500).json({
      error: "APP_PASSWORD 환경변수가 설정되지 않았습니다.",
    });
  }

  if (password === appPassword) {
    return res.status(200).json({ success: true });
  }

  return res.status(401).json({ success: false, error: "비밀번호가 틀렸습니다." });
};

