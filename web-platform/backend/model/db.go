package model

import (
	"database/sql"
	"time"

	_ "github.com/mattn/go-sqlite3"
)

type Task struct {
	ID         string `json:"id"`
	VideoPath  string `json:"video_path"`
	Params     string `json:"params"`
	Status     string `json:"status"`
	Progress   int    `json:"progress"`
	Result     string `json:"result"`
	Error      string `json:"error"`
	CreatedAt  string `json:"created_at"`
	UpdatedAt  string `json:"updated_at"`
}

var db *sql.DB

func InitDB(path string) error {
	var err error
	db, err = sql.Open("sqlite3", path+"?_journal_mode=WAL")
	if err != nil {
		return err
	}
	_, err = db.Exec(`
		CREATE TABLE IF NOT EXISTS workflow_tasks (
			id TEXT PRIMARY KEY,
			video_path TEXT,
			params TEXT,
			status TEXT DEFAULT 'pending',
			progress INTEGER DEFAULT 0,
			result TEXT,
			error TEXT,
			created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
			updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
		)
	`)
	return err
}

func GetTask(id string) (*Task, error) {
	t := &Task{}
	err := db.QueryRow(
		`SELECT id, video_path, params, status, progress, result, error, created_at, updated_at
		 FROM workflow_tasks WHERE id = ?`, id,
	).Scan(&t.ID, &t.VideoPath, &t.Params, &t.Status, &t.Progress, &t.Result, &t.Error, &t.CreatedAt, &t.UpdatedAt)
	if err != nil {
		return nil, err
	}
	return t, nil
}

func CreateTask(t *Task) error {
	now := time.Now().UTC().Format(time.RFC3339)
	t.CreatedAt = now
	t.UpdatedAt = now
	_, err := db.Exec(
		`INSERT INTO workflow_tasks (id, video_path, params, status, progress, created_at, updated_at)
		 VALUES (?, ?, ?, ?, ?, ?, ?)`,
		t.ID, t.VideoPath, t.Params, t.Status, t.Progress, t.CreatedAt, t.UpdatedAt,
	)
	return err
}

func UpdateTask(t *Task) error {
	t.UpdatedAt = time.Now().UTC().Format(time.RFC3339)
	_, err := db.Exec(
		`UPDATE workflow_tasks SET status=?, progress=?, result=?, error=?, updated_at=?
		 WHERE id=?`,
		t.Status, t.Progress, t.Result, t.Error, t.UpdatedAt, t.ID,
	)
	return err
}
