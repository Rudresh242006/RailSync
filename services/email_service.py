import smtplib
import secrets
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def generate_otp() -> str:
    """Generate a cryptographically secure 6-digit OTP."""
    return str(secrets.randbelow(900000) + 100000)


def send_otp_email(to_email: str, name: str, otp: str) -> bool:
    """
    Send an OTP verification email using Gmail SMTP.
    Returns True on success, False on failure.
    """
    mail_user = os.environ.get('MAIL_USERNAME', '')
    mail_pass = os.environ.get('MAIL_PASSWORD', '')

    if not mail_user or not mail_pass or 'your-gmail' in mail_user:
        # Dev fallback: just print the OTP so developers can test without email
        print(f"\n{'='*50}")
        print(f"[DEV MODE] OTP for {to_email}: {otp}")
        print(f"{'='*50}\n")
        return True  # Return True so flow continues in dev

    html_body = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <style>
    body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #08090e; margin: 0; padding: 20px; }}
    .container {{ max-width: 480px; margin: 0 auto; background: #0f1019; border-radius: 16px;
                  border: 1px solid rgba(108,99,255,0.2); overflow: hidden; }}
    .header {{ background: linear-gradient(135deg, #6c63ff, #5046e5); padding: 32px 40px; text-align: center; }}
    .header h1 {{ color: #fff; margin: 0; font-size: 26px; letter-spacing: -0.5px; }}
    .header p {{ color: rgba(255,255,255,0.75); margin: 8px 0 0; font-size: 14px; }}
    .body {{ padding: 36px 40px; }}
    .greeting {{ color: #e8eaf0; font-size: 15px; margin-bottom: 20px; }}
    .otp-box {{ background: rgba(108,99,255,0.08); border: 2px solid rgba(108,99,255,0.3);
                border-radius: 12px; padding: 24px; text-align: center; margin: 24px 0; }}
    .otp-code {{ font-size: 42px; font-weight: 800; color: #a89cff; letter-spacing: 10px; }}
    .otp-label {{ color: #8b8fa8; font-size: 12px; margin-top: 8px; text-transform: uppercase; letter-spacing: 1px; }}
    .note {{ color: #8b8fa8; font-size: 13px; line-height: 1.6; }}
    .note strong {{ color: #ef4444; }}
    .footer {{ background: rgba(0,0,0,0.2); padding: 20px 40px; text-align: center; 
               color: #4a4d6a; font-size: 12px; border-top: 1px solid rgba(255,255,255,0.05); }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>🚆 RailSync</h1>
      <p>Email Verification</p>
    </div>
    <div class="body">
      <p class="greeting">Hi <strong style="color:#a89cff">{name}</strong>,</p>
      <p class="note">You're almost there! Use the verification code below to confirm your email address and activate your RailSync passenger account.</p>
      <div class="otp-box">
        <div class="otp-code">{otp}</div>
        <div class="otp-label">Your One-Time Password</div>
      </div>
      <p class="note">
        This OTP is valid for <strong>10 minutes</strong>. 
        If you did not create an account on RailSync, please ignore this email.
      </p>
    </div>
    <div class="footer">
      &copy; 2024 RailSync. Secure railway booking platform.
    </div>
  </div>
</body>
</html>
    """

    msg = MIMEMultipart('alternative')
    msg['Subject'] = f'RailSync — Your verification code is {otp}'
    msg['From']    = f'RailSync <{mail_user}>'
    msg['To']      = to_email

    msg.attach(MIMEText(html_body, 'html'))

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(mail_user, mail_pass)
            server.sendmail(mail_user, to_email, msg.as_string())
        return True
    except Exception as e:
        print(f"[EMAIL ERROR] Failed to send OTP: {e}")
        return False
