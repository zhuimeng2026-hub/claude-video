package config

import (
	"os"
	"strconv"
)

type Config struct {
	Server   ServerConfig
	Database DatabaseConfig
	Pipeline PipelineConfig
}

type ServerConfig struct {
	Port int
}

type DatabaseConfig struct {
	Path string
}

type PipelineConfig struct {
	PythonBin string
	ScriptDir string
	WorkDir   string
}

func Load() *Config {
	cfg := &Config{
		Server: ServerConfig{Port: 8080},
		Database: DatabaseConfig{Path: "workflow.db"},
		Pipeline: PipelineConfig{
			PythonBin: "python3",
			ScriptDir: "../pipeline",
			WorkDir:   "/opt/claude-video/web-platform/work",
		},
	}
	if v := os.Getenv("SERVER_PORT"); v != "" {
		if p, err := strconv.Atoi(v); err == nil {
			cfg.Server.Port = p
		}
	}
	if v := os.Getenv("DB_PATH"); v != "" {
		cfg.Database.Path = v
	}
	if v := os.Getenv("PYTHON_BIN"); v != "" {
		cfg.Pipeline.PythonBin = v
	}
	if v := os.Getenv("PIPELINE_SCRIPT_DIR"); v != "" {
		cfg.Pipeline.ScriptDir = v
	}
	if v := os.Getenv("PIPELINE_WORK_DIR"); v != "" {
		cfg.Pipeline.WorkDir = v
	}
	return cfg
}
