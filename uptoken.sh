sqlite3 /Users/kooper/git/ncsa/llm/instance/illm_dev.db "
  INSERT INTO model_limits (entity_id, model_config_id, token_limit, tokens_per_hour, tokens_left, last_refill_at)
  SELECT e.id, mc.id, 10000, 10000, 10000, datetime('now')
  FROM entities e
  CROSS JOIN model_configs mc
  WHERE e.entity_type = 'user'
    AND e.active = 1
    AND mc.active = 1
  ON CONFLICT (entity_id, model_config_id) DO UPDATE
    SET token_limit     = 10000,
        tokens_per_hour = 10000,
        tokens_left     = 10000;
  "
