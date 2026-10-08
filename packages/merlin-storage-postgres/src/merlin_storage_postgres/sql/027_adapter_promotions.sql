ALTER TABLE merlin_storage.promotion_candidates
DROP CONSTRAINT promotion_candidates_kind_check;

ALTER TABLE merlin_storage.promotion_candidates
ADD CONSTRAINT promotion_candidates_kind_check
CHECK (kind IN ('skill', 'guidance', 'lesson', 'routing', 'profile', 'adapter'));
