-- init.sql выполняется один раз при первом старте контейнера.
-- Создаёт пользователя приложения с правом USAGE на БД и схему public.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT FROM pg_roles WHERE rolname = 'app_user'
    ) THEN
        CREATE ROLE app_user LOGIN PASSWORD 'user_secret';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE appdb TO app_user;
GRANT USAGE ON SCHEMA public TO app_user;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO app_user;
