-- Schema for Evolving Multi-Agent Teaching Assistant (darwin)

CREATE TABLE IF NOT EXISTS teachers (
    teacher_id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(100) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    full_name VARCHAR(150) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS agents (
    agent_id INT AUTO_INCREMENT PRIMARY KEY,
    generation INT NOT NULL DEFAULT 1,
    parent_id INT NULL,
    strategy_prompt TEXT NOT NULL,
    status ENUM('active', 'retired') NOT NULL DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    retired_at TIMESTAMP NULL,
    retirement_reason TEXT NULL,
    mistake_summary TEXT NULL,
    reproduction_reason TEXT NULL,
    success_rationale TEXT NULL,
    times_used INT NOT NULL DEFAULT 0,
    avg_score FLOAT NOT NULL DEFAULT 0.0,
    INDEX idx_agents_status (status),
    INDEX idx_agents_avg_score (avg_score),
    CONSTRAINT fk_agents_parent FOREIGN KEY (parent_id) REFERENCES agents(agent_id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS knowledge_pool (
    knowledge_id INT AUTO_INCREMENT PRIMARY KEY,
    agent_id INT NOT NULL,
    topic VARCHAR(255) NOT NULL,
    student_level VARCHAR(50) NOT NULL,
    feedback_score INT NOT NULL,
    feedback_comment TEXT NULL,
    outcome_summary TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_knowledge_topic (topic),
    INDEX idx_knowledge_score (feedback_score),
    CONSTRAINT fk_knowledge_agent FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS feedback (
    feedback_id INT AUTO_INCREMENT PRIMARY KEY,
    agent_id INT NOT NULL,
    teacher_id INT NULL,
    topic VARCHAR(255) NOT NULL,
    student_level VARCHAR(50) NOT NULL,
    score INT NOT NULL,
    comment TEXT NULL,
    response_quality_score TINYINT NULL,
    student_improvement_score TINYINT NULL,
    student_improvement TEXT NULL,
    what_worked TEXT NULL,
    what_to_improve TEXT NULL,
    follow_up_action TEXT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_feedback_agent (agent_id),
    INDEX idx_feedback_teacher (teacher_id),
    CONSTRAINT fk_feedback_agent FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE,
    CONSTRAINT fk_feedback_teacher FOREIGN KEY (teacher_id) REFERENCES teachers(teacher_id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS evolution_log (
    log_id INT AUTO_INCREMENT PRIMARY KEY,
    event_type VARCHAR(50) NOT NULL,
    details TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS soup_training_runs (
    run_id INT AUTO_INCREMENT PRIMARY KEY,
    feedback_count INT NOT NULL,
    example_count INT NOT NULL DEFAULT 0,
    status ENUM('queued', 'running', 'completed', 'failed', 'disabled') NOT NULL DEFAULT 'queued',
    dataset_path VARCHAR(1024) NOT NULL,
    config_path VARCHAR(1024) NOT NULL,
    output_path VARCHAR(1024) NOT NULL,
    log_path VARCHAR(1024) NULL,
    pid BIGINT NULL,
    error_message TEXT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP NULL,
    finished_at TIMESTAMP NULL,
    UNIQUE KEY uq_soup_feedback_count (feedback_count),
    INDEX idx_soup_training_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
