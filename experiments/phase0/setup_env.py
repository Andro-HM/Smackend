"""Creates local credentials without overwriting existing ones."""

from pathlib import Path
from secrets import token_urlsafe

root = Path(__file__).resolve().parent
template = (root / ".env.example").read_text(encoding="utf-8")
try:
    with (root / ".env").open("x", encoding="utf-8") as env:
        env.write(
            template.replace("POSTGRES_PASSWORD=\n", f"POSTGRES_PASSWORD={token_urlsafe(32)}\n")
            .replace("DJANGO_SECRET_KEY=\n", f"DJANGO_SECRET_KEY={token_urlsafe(48)}\n")
        )
except FileExistsError:
    print(".env already exists.")
else:
    print("Created .env with local credentials.")
