ALTER TABLE attempts ADD COLUMN queue_seconds DOUBLE PRECISION;

CREATE TABLE checkpoints (
	id VARCHAR(36) NOT NULL,
	step_id VARCHAR(36) NOT NULL,
	producer_attempt_id VARCHAR(36) NOT NULL,
	chunk_index INTEGER NOT NULL,
	chunk_key VARCHAR(64) NOT NULL,
	identity JSONB NOT NULL,
	directory VARCHAR(200) NOT NULL,
	manifest_digest VARCHAR(64) NOT NULL,
	content_digest VARCHAR(64) NOT NULL,
	manifest JSONB NOT NULL,
	epoch INTEGER NOT NULL,
	fence BIGINT NOT NULL,
	valid BOOLEAN NOT NULL,
	committed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (producer_attempt_id, chunk_index),
	FOREIGN KEY(step_id) REFERENCES steps (id),
	FOREIGN KEY(producer_attempt_id) REFERENCES attempts (id),
	UNIQUE (directory)
);

CREATE INDEX ix_checkpoint_lookup ON checkpoints (step_id, chunk_key, valid);

CREATE TABLE resource_samples (
	id VARCHAR(36) NOT NULL,
	slot_id VARCHAR(150) NOT NULL,
	measured_at TIMESTAMP WITH TIME ZONE NOT NULL,
	resolution VARCHAR(10) NOT NULL,
	status VARCHAR(20) NOT NULL,
	holder_id VARCHAR(36),
	sample JSONB NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (slot_id, measured_at, resolution),
	FOREIGN KEY(slot_id) REFERENCES resource_slots (id),
	FOREIGN KEY(holder_id) REFERENCES attempts (id)
);

CREATE INDEX ix_resource_samples_time ON resource_samples (measured_at);

CREATE INDEX ix_resource_history ON resource_samples (slot_id, measured_at);

CREATE TABLE checkpoint_uses (
	id VARCHAR(36) NOT NULL,
	attempt_id VARCHAR(36) NOT NULL,
	checkpoint_id VARCHAR(36) NOT NULL,
	chunk_index INTEGER NOT NULL,
	reused BOOLEAN NOT NULL,
	at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (attempt_id, chunk_index),
	FOREIGN KEY(attempt_id) REFERENCES attempts (id),
	FOREIGN KEY(checkpoint_id) REFERENCES checkpoints (id)
);
