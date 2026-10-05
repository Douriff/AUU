"""Verification-code email texts per interface language (zh-CN is the original wording).

{brand} = AUUTRADE (never translated), {code} = the 6-digit code, {m} = minutes valid."""
from __future__ import annotations

EMAIL_LANGS = ("zh-CN", "en", "de", "fr", "es", "pt", "tr", "ru", "ja", "ko", "ar")
RTL = {"ar"}

TEXTS: dict[str, dict[str, str]] = {
    "zh-CN": {
        "signup_subject": "{brand} 注册验证码：{code}",
        "signup_action": "您正在注册 {brand} 账号，本次验证码为：",
        "signup_ignore": "如果这不是您本人的操作，请忽略本邮件，您的邮箱不会被绑定。",
        "reset_subject": "{brand} 重置密码验证码：{code}",
        "reset_action": "您正在重置 {brand} 账号的登录密码，本次验证码为：",
        "reset_ignore": "如果这不是您本人的操作，请忽略本邮件，并建议尽快修改邮箱密码。",
        "validity": "验证码 {m} 分钟内有效，请勿泄露给任何人。{brand} 工作人员不会以任何理由向您索要验证码。",
        "hello": "您好，",
        "team": "—— {brand} 团队",
        "auto": "此邮件由系统自动发送，请勿直接回复。",
    },
    "en": {
        "signup_subject": "{brand} sign-up verification code: {code}",
        "signup_action": "You are creating a {brand} account. Your verification code is:",
        "signup_ignore": "If you did not request this, please ignore this email. Your address will not be linked to any account.",
        "reset_subject": "{brand} password reset code: {code}",
        "reset_action": "You are resetting the password of your {brand} account. Your verification code is:",
        "reset_ignore": "If you did not request this, please ignore this email. We recommend changing your email password soon.",
        "validity": "The code is valid for {m} minutes. Do not share it with anyone. {brand} staff will never ask you for this code.",
        "hello": "Hello,",
        "team": "— The {brand} Team",
        "auto": "This is an automated message. Please do not reply.",
    },
    "de": {
        "signup_subject": "{brand} Bestätigungscode für die Registrierung: {code}",
        "signup_action": "Sie registrieren ein {brand}-Konto. Ihr Bestätigungscode lautet:",
        "signup_ignore": "Wenn Sie das nicht waren, ignorieren Sie diese E-Mail. Ihre Adresse wird mit keinem Konto verknüpft.",
        "reset_subject": "{brand} Code zum Zurücksetzen des Passworts: {code}",
        "reset_action": "Sie setzen das Passwort Ihres {brand}-Kontos zurück. Ihr Bestätigungscode lautet:",
        "reset_ignore": "Wenn Sie das nicht waren, ignorieren Sie diese E-Mail. Wir empfehlen, bald das Passwort Ihres E-Mail-Kontos zu ändern.",
        "validity": "Der Code ist {m} Minuten gültig. Geben Sie ihn an niemanden weiter. Mitarbeitende von {brand} werden Sie niemals nach diesem Code fragen.",
        "hello": "Hallo,",
        "team": "— Ihr {brand}-Team",
        "auto": "Diese E-Mail wurde automatisch versendet. Bitte antworten Sie nicht darauf.",
    },
    "fr": {
        "signup_subject": "Code de vérification d’inscription {brand} : {code}",
        "signup_action": "Vous créez un compte {brand}. Votre code de vérification est :",
        "signup_ignore": "Si vous n’êtes pas à l’origine de cette demande, ignorez cet e-mail. Votre adresse ne sera associée à aucun compte.",
        "reset_subject": "Code de réinitialisation du mot de passe {brand} : {code}",
        "reset_action": "Vous réinitialisez le mot de passe de votre compte {brand}. Votre code de vérification est :",
        "reset_ignore": "Si vous n’êtes pas à l’origine de cette demande, ignorez cet e-mail. Nous vous conseillons de changer rapidement le mot de passe de votre messagerie.",
        "validity": "Ce code est valable {m} minutes. Ne le communiquez à personne. L’équipe {brand} ne vous demandera jamais ce code.",
        "hello": "Bonjour,",
        "team": "— L’équipe {brand}",
        "auto": "Cet e-mail a été envoyé automatiquement, merci de ne pas y répondre.",
    },
    "es": {
        "signup_subject": "Código de verificación de registro de {brand}: {code}",
        "signup_action": "Estás creando una cuenta de {brand}. Tu código de verificación es:",
        "signup_ignore": "Si no has sido tú, ignora este correo. Tu dirección no se vinculará a ninguna cuenta.",
        "reset_subject": "Código para restablecer la contraseña de {brand}: {code}",
        "reset_action": "Estás restableciendo la contraseña de tu cuenta de {brand}. Tu código de verificación es:",
        "reset_ignore": "Si no has sido tú, ignora este correo. Te recomendamos cambiar pronto la contraseña de tu correo electrónico.",
        "validity": "El código es válido durante {m} minutos. No lo compartas con nadie. El personal de {brand} nunca te pedirá este código.",
        "hello": "Hola:",
        "team": "— El equipo de {brand}",
        "auto": "Este correo se ha enviado automáticamente. Por favor, no respondas.",
    },
    "pt": {
        "signup_subject": "Código de verificação de cadastro {brand}: {code}",
        "signup_action": "Você está criando uma conta {brand}. Seu código de verificação é:",
        "signup_ignore": "Se não foi você, ignore este e-mail. Seu endereço não será vinculado a nenhuma conta.",
        "reset_subject": "Código de redefinição de senha {brand}: {code}",
        "reset_action": "Você está redefinindo a senha da sua conta {brand}. Seu código de verificação é:",
        "reset_ignore": "Se não foi você, ignore este e-mail. Recomendamos alterar a senha do seu e-mail em breve.",
        "validity": "O código é válido por {m} minutos. Não o compartilhe com ninguém. A equipe {brand} nunca pedirá este código.",
        "hello": "Olá,",
        "team": "— Equipe {brand}",
        "auto": "Este e-mail foi enviado automaticamente. Por favor, não responda.",
    },
    "tr": {
        "signup_subject": "{brand} kayıt doğrulama kodu: {code}",
        "signup_action": "Bir {brand} hesabı oluşturuyorsunuz. Doğrulama kodunuz:",
        "signup_ignore": "Bu işlemi siz yapmadıysanız bu e-postayı dikkate almayın. Adresiniz hiçbir hesaba bağlanmayacaktır.",
        "reset_subject": "{brand} şifre sıfırlama kodu: {code}",
        "reset_action": "{brand} hesabınızın şifresini sıfırlıyorsunuz. Doğrulama kodunuz:",
        "reset_ignore": "Bu işlemi siz yapmadıysanız bu e-postayı dikkate almayın ve e-posta şifrenizi en kısa sürede değiştirmenizi öneririz.",
        "validity": "Kod {m} dakika geçerlidir. Kimseyle paylaşmayın. {brand} çalışanları bu kodu asla sizden istemez.",
        "hello": "Merhaba,",
        "team": "— {brand} Ekibi",
        "auto": "Bu e-posta otomatik olarak gönderilmiştir, lütfen yanıtlamayın.",
    },
    "ru": {
        "signup_subject": "Код подтверждения регистрации {brand}: {code}",
        "signup_action": "Вы регистрируете аккаунт {brand}. Ваш код подтверждения:",
        "signup_ignore": "Если это были не вы, просто проигнорируйте письмо — ваш адрес не будет привязан к аккаунту.",
        "reset_subject": "Код для сброса пароля {brand}: {code}",
        "reset_action": "Вы сбрасываете пароль аккаунта {brand}. Ваш код подтверждения:",
        "reset_ignore": "Если это были не вы, проигнорируйте письмо и рекомендуем как можно скорее сменить пароль от почты.",
        "validity": "Код действителен {m} мин. Никому его не сообщайте. Сотрудники {brand} никогда не запрашивают этот код.",
        "hello": "Здравствуйте!",
        "team": "— Команда {brand}",
        "auto": "Письмо отправлено автоматически, отвечать на него не нужно.",
    },
    "ja": {
        "signup_subject": "{brand} 登録用確認コード：{code}",
        "signup_action": "{brand} アカウントを登録しようとしています。確認コードは次のとおりです：",
        "signup_ignore": "お心当たりがない場合は、このメールを無視してください。メールアドレスがアカウントに紐づけられることはありません。",
        "reset_subject": "{brand} パスワード再設定用確認コード：{code}",
        "reset_action": "{brand} アカウントのパスワードを再設定しようとしています。確認コードは次のとおりです：",
        "reset_ignore": "お心当たりがない場合は、このメールを無視し、早めにメールアカウントのパスワードを変更することをおすすめします。",
        "validity": "確認コードの有効期限は {m} 分です。他人に教えないでください。{brand} のスタッフがコードを尋ねることは絶対にありません。",
        "hello": "こんにちは。",
        "team": "—— {brand} チーム",
        "auto": "このメールは送信専用です。返信しないでください。",
    },
    "ko": {
        "signup_subject": "{brand} 회원가입 인증 코드: {code}",
        "signup_action": "{brand} 계정을 가입하고 있습니다. 인증 코드는 다음과 같습니다:",
        "signup_ignore": "본인이 요청하지 않았다면 이 메일을 무시하세요. 이메일 주소는 어떤 계정에도 연결되지 않습니다.",
        "reset_subject": "{brand} 비밀번호 재설정 인증 코드: {code}",
        "reset_action": "{brand} 계정의 비밀번호를 재설정하고 있습니다. 인증 코드는 다음과 같습니다:",
        "reset_ignore": "본인이 요청하지 않았다면 이 메일을 무시하고, 가능한 한 빨리 이메일 계정 비밀번호를 변경하시기 바랍니다.",
        "validity": "인증 코드는 {m}분 동안 유효합니다. 누구에게도 알려주지 마세요. {brand} 직원은 어떤 이유로도 인증 코드를 요구하지 않습니다.",
        "hello": "안녕하세요.",
        "team": "— {brand} 팀",
        "auto": "이 메일은 발신 전용입니다. 회신하지 마세요.",
    },
    "ar": {
        "signup_subject": "رمز التحقق للتسجيل في {brand}: {code}",
        "signup_action": "أنت تقوم بإنشاء حساب في {brand}. رمز التحقق الخاص بك هو:",
        "signup_ignore": "إذا لم تطلب ذلك، فتجاهل هذه الرسالة، ولن يُربط بريدك الإلكتروني بأي حساب.",
        "reset_subject": "رمز إعادة تعيين كلمة المرور في {brand}: {code}",
        "reset_action": "أنت تقوم بإعادة تعيين كلمة مرور حسابك في {brand}. رمز التحقق الخاص بك هو:",
        "reset_ignore": "إذا لم تطلب ذلك، فتجاهل هذه الرسالة، وننصحك بتغيير كلمة مرور بريدك الإلكتروني في أقرب وقت.",
        "validity": "الرمز صالح لمدة {m} دقائق. لا تشاركه مع أي شخص. لن يطلب منك موظفو {brand} هذا الرمز أبدًا.",
        "hello": "مرحبًا،",
        "team": "— فريق {brand}",
        "auto": "تم إرسال هذه الرسالة تلقائيًا، يُرجى عدم الرد عليها.",
    },
}


def email_lang(lang: object) -> str:
    """Map a client language tag to a supported email language (default zh-CN)."""
    if not isinstance(lang, str):
        return "zh-CN"
    tag = lang.strip()
    if tag in TEXTS:
        return tag
    base = tag.split("-")[0].lower()
    if base == "zh":
        return "zh-CN"
    return base if base in TEXTS else "zh-CN"
