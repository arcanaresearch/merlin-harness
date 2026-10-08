DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.schemata WHERE schema_name = 'redpen_storage'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.schemata WHERE schema_name = 'merlin_storage'
    ) THEN
        ALTER SCHEMA redpen_storage RENAME TO merlin_storage;
    END IF;
END $$;
