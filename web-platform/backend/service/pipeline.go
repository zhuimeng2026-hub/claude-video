package service

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"

	"video-workflow/model"
)

// BroadcastFunc is called to push progress updates to connected WebSocket clients.
// Set by main.go to avoid import cycle with handler package.
var BroadcastFunc func(taskID string, data map[string]interface{})

type ProgressUpdate struct {
	Status   string      `json:"status"`
	Progress int         `json:"progress"`
	Message  string      `json:"message"`
	Result   interface{} `json:"result,omitempty"`
	Error    string      `json:"error,omitempty"`
}

func StartPipeline(taskID string, paramsJSON string) {
	task, err := model.GetTask(taskID)
	if err != nil {
		return
	}

	scriptPath := filepath.Join("../pipeline", "run_pipeline.py")
	pythonBin := "python3"

	cmd := exec.Command(pythonBin, scriptPath, "--task-id", taskID, "--params", paramsJSON)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		failTask(task, err.Error())
		return
	}
	cmd.Stderr = os.Stderr

	if err := cmd.Start(); err != nil {
		failTask(task, err.Error())
		return
	}

	scanner := bufio.NewScanner(stdout)
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" {
			continue
		}
		var update ProgressUpdate
		if err := json.Unmarshal([]byte(line), &update); err != nil {
			continue
		}

		task.Status = update.Status
		task.Progress = update.Progress
		if update.Error != "" {
			task.Error = update.Error
		}
		if update.Result != nil {
			resultBytes, _ := json.Marshal(update.Result)
			task.Result = string(resultBytes)
		}
		model.UpdateTask(task)

		if BroadcastFunc != nil {
			BroadcastFunc(taskID, map[string]interface{}{
				"status":   update.Status,
				"progress": update.Progress,
				"message":  update.Message,
			})
		}
	}

	cmd.Wait()

	if task.Status != "completed" && task.Status != "failed" {
		task.Status = "completed"
		task.Progress = 100
		model.UpdateTask(task)
		if BroadcastFunc != nil {
			BroadcastFunc(taskID, map[string]interface{}{
				"status":   "completed",
				"progress": 100,
				"message":  "Done",
			})
		}
	}
}

func failTask(task *model.Task, msg string) {
	task.Status = "failed"
	task.Error = msg
	model.UpdateTask(task)
	if BroadcastFunc != nil {
		BroadcastFunc(task.ID, map[string]interface{}{
			"status":  "failed",
			"error":   msg,
			"message": fmt.Sprintf("Pipeline failed: %s", msg),
		})
	}
}
