-- Frozen PostgreSQL schema for revision 0001.

CREATE TABLE tasks (
	id VARCHAR(36) NOT NULL,
	idempotency_key VARCHAR(128) NOT NULL,
	request_hash VARCHAR(64) NOT NULL,
	request JSONB NOT NULL,
	input_digest VARCHAR(64) NOT NULL,
	pipeline JSONB NOT NULL,
	status VARCHAR(20) NOT NULL,
	epoch INTEGER NOT NULL,
	revision INTEGER NOT NULL,
	event_seq INTEGER NOT NULL,
	stop_reason TEXT,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	epoch_started_at TIMESTAMP WITH TIME ZONE NOT NULL,
	deadline_at TIMESTAMP WITH TIME ZONE NOT NULL,
	finished_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	UNIQUE (idempotency_key)
);

CREATE TABLE worker_sessions (
	id VARCHAR(36) NOT NULL,
	node_id VARCHAR(100) NOT NULL,
	pid INTEGER NOT NULL,
	process_created FLOAT NOT NULL,
	host_name VARCHAR(100) NOT NULL,
	heartbeat_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE resource_slots (
	id VARCHAR(150) NOT NULL,
	node_id VARCHAR(100) NOT NULL,
	key VARCHAR(30) NOT NULL,
	kind VARCHAR(10) NOT NULL,
	status VARCHAR(20) NOT NULL,
	holder_id VARCHAR(36),
	holder_fence BIGINT,
	reason TEXT,
	sample JSONB NOT NULL,
	measured_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	UNIQUE (node_id, key),
	CONSTRAINT slot_holder_state CHECK ((status = 'AVAILABLE' AND holder_id IS NULL) OR (status IN ('BUSY','QUARANTINED') AND holder_id IS NOT NULL)),
	UNIQUE (holder_id)
);

CREATE TABLE steps (
	id VARCHAR(36) NOT NULL,
	task_id VARCHAR(36) NOT NULL,
	key VARCHAR(40) NOT NULL,
	spec JSONB NOT NULL,
	status VARCHAR(20) NOT NULL,
	fence BIGINT NOT NULL,
	current_attempt_id VARCHAR(36),
	next_run_at TIMESTAMP WITH TIME ZONE,
	ready_at TIMESTAMP WITH TIME ZONE,
	wait_reason VARCHAR(60),
	error TEXT,
	artifact_id VARCHAR(36),
	cache_key VARCHAR(64),
	progress FLOAT NOT NULL,
	last_progress_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	UNIQUE (task_id, key),
	FOREIGN KEY(task_id) REFERENCES tasks (id)
);

CREATE INDEX ix_step_ready ON steps (status, next_run_at);

CREATE TABLE task_events (
	id VARCHAR(36) NOT NULL,
	task_id VARCHAR(36) NOT NULL,
	seq INTEGER NOT NULL,
	epoch INTEGER NOT NULL,
	type VARCHAR(80) NOT NULL,
	step_key VARCHAR(40),
	attempt_id VARCHAR(36),
	at TIMESTAMP WITH TIME ZONE NOT NULL,
	payload JSONB NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (task_id, seq),
	FOREIGN KEY(task_id) REFERENCES tasks (id)
);

CREATE TABLE command_receipts (
	id VARCHAR(36) NOT NULL,
	task_id VARCHAR(36) NOT NULL,
	action VARCHAR(30) NOT NULL,
	operation_key VARCHAR(128) NOT NULL,
	request_hash VARCHAR(64) NOT NULL,
	response JSONB NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (task_id, action, operation_key),
	FOREIGN KEY(task_id) REFERENCES tasks (id)
);

CREATE TABLE attempts (
	id VARCHAR(36) NOT NULL,
	step_id VARCHAR(36) NOT NULL,
	task_id VARCHAR(36) NOT NULL,
	number INTEGER NOT NULL,
	epoch INTEGER NOT NULL,
	fence BIGINT NOT NULL,
	worker_id VARCHAR(36) NOT NULL,
	slot_id VARCHAR(150) NOT NULL,
	status VARCHAR(20) NOT NULL,
	phase VARCHAR(20) NOT NULL,
	claimed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE,
	heartbeat_at TIMESTAMP WITH TIME ZONE NOT NULL,
	lease_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	run_deadline_at TIMESTAMP WITH TIME ZONE,
	finished_at TIMESTAMP WITH TIME ZONE,
	error_code VARCHAR(80),
	error TEXT,
	retryable BOOLEAN NOT NULL,
	executor_pid INTEGER,
	executor_created FLOAT,
	cleanup_state VARCHAR(20) NOT NULL,
	manifest_digest VARCHAR(64),
	PRIMARY KEY (id),
	UNIQUE (step_id, number),
	FOREIGN KEY(step_id) REFERENCES steps (id),
	FOREIGN KEY(task_id) REFERENCES tasks (id),
	FOREIGN KEY(worker_id) REFERENCES worker_sessions (id)
);

CREATE UNIQUE INDEX uq_running_step ON attempts (step_id) WHERE status = 'RUNNING';

CREATE INDEX ix_attempt_lease ON attempts (status, lease_expires_at);

CREATE TABLE artifacts (
	id VARCHAR(36) NOT NULL,
	producer_attempt_id VARCHAR(36) NOT NULL,
	directory VARCHAR(200) NOT NULL,
	manifest_digest VARCHAR(64) NOT NULL,
	content_digest VARCHAR(64) NOT NULL,
	manifest JSONB NOT NULL,
	valid BOOLEAN NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (producer_attempt_id),
	FOREIGN KEY(producer_attempt_id) REFERENCES attempts (id),
	UNIQUE (directory)
);

CREATE TABLE cache_entries (
	key VARCHAR(64) NOT NULL,
	artifact_id VARCHAR(36) NOT NULL,
	PRIMARY KEY (key),
	FOREIGN KEY(artifact_id) REFERENCES artifacts (id)
);

ALTER TABLE resource_slots ADD FOREIGN KEY(holder_id) REFERENCES attempts (id);

ALTER TABLE steps ADD FOREIGN KEY(artifact_id) REFERENCES artifacts (id);

ALTER TABLE attempts ADD FOREIGN KEY(slot_id) REFERENCES resource_slots (id);
