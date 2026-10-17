#!/usr/bin/env bash
# Install or update the bot on a fresh Ubuntu/Debian server.
#   curl -fsSL https://raw.githubusercontent.com/v1teka/agent/main/deploy/install.sh | sudo bash
# Re-running it updates the bot to the latest code and keeps your .env and data.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/v1teka/agent.git}"
APP_DIR="${APP_DIR:-/opt/agent}"

say() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mОшибка: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "запусти скрипт от root (через sudo)."
command -v apt-get >/dev/null || die "скрипт рассчитан на Ubuntu или Debian."

# Questions are read from the terminal even when the script itself comes from a pipe.
ask() {  # ask VAR "prompt" [default] [secret|optional]
  local var="$1" prompt="$2" default="${3:-}" mode="${4:-}" value=""
  while :; do
    if [ "$mode" = "secret" ]; then
      read -r -s -p "$prompt: " value </dev/tty; echo
    else
      read -r -p "$prompt${default:+ [$default]}: " value </dev/tty
    fi
    value="${value:-$default}"
    if [ -n "$value" ] || [ "$mode" = "optional" ]; then break; fi
  done
  printf -v "$var" '%s' "$value"
}

say "Ставлю git, curl и Docker"
apt-get update -qq
apt-get install -y -qq git curl ca-certificates >/dev/null
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh >/dev/null
fi
systemctl enable --now docker >/dev/null 2>&1 || true

say "Скачиваю код в $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only -q
else
  git clone -q "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"

if [ ! -f .env ]; then
  say "Настройка. Ключи сохраняются только на этом сервере, в $APP_DIR/.env"
  ask TELEGRAM_BOT_TOKEN "Токен бота от @BotFather" "" secret
  ask ANTHROPIC_API_KEY "API-ключ Claude (sk-ant-...)" "" secret
  ask TIMEZONE "Часовой пояс" "Europe/Moscow"
  ask ALLOWED_USER_IDS "Твой Telegram id (можно оставить пустым и добавить позже)" "" optional
  cp .env.example .env
  sed -i \
    -e "s|^TELEGRAM_BOT_TOKEN=.*|TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN|" \
    -e "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY|" \
    -e "s|^TIMEZONE=.*|TIMEZONE=$TIMEZONE|" \
    -e "s|^ALLOWED_USER_IDS=.*|ALLOWED_USER_IDS=$ALLOWED_USER_IDS|" \
    .env
  chmod 600 .env
fi

set -a; . ./.env; set +a

say "Проверяю, что сервер достаёт до Telegram и Claude"
tg=$(curl -s -o /dev/null -w '%{http_code}' "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" || echo 000)
[ "$tg" = "200" ] || die "Telegram не принял токен (код $tg). Проверь TELEGRAM_BOT_TOKEN в $APP_DIR/.env и запусти скрипт ещё раз."
cl=$(curl -s -o /tmp/claude-check.json -w '%{http_code}' https://api.anthropic.com/v1/models \
  -H "x-api-key: ${ANTHROPIC_API_KEY}" -H "anthropic-version: 2023-06-01" || echo 000)
case "$cl" in
  200) ;;
  401) die "Claude не принял ключ. Проверь ANTHROPIC_API_KEY в $APP_DIR/.env и запусти скрипт ещё раз." ;;
  403) die "Claude отказал в доступе с этого сервера (код 403). Скорее всего, страна сервера не поддерживается: возьми VPS в другой стране." ;;
  *)   die "Не удалось достучаться до Claude (код $cl): $(head -c 300 /tmp/claude-check.json 2>/dev/null)" ;;
esac
echo "Telegram и Claude отвечают."

say "Собираю и запускаю бота"
mkdir -p data
docker compose up -d --build

say "Готово. Напиши своему боту в Telegram /start"
cat <<MSG
Полезное:
  логи:            cd $APP_DIR && docker compose logs -f
  перезапуск:      cd $APP_DIR && docker compose restart
  настройки:       nano $APP_DIR/.env, затем перезапуск
  обновить бота:   запусти эту же команду установки ещё раз
MSG
