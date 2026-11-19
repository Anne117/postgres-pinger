# Разрешаем сервису только читать конкретный секрет PostgreSQL.
path "secret/data/postgres" {
  capabilities = ["read"]
}
