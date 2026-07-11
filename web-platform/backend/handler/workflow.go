package handler

import (
	"encoding/json"
	"fmt"
	"net/http"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/gorilla/websocket"

	"video-workflow/model"
	"video-workflow/service"
)

var upgrader = websocket.Upgrader{
	CheckOrigin: func(r *http.Request) bool { return true },
}

var wsClients = make(map[string][]*websocket.Conn)
var wsMu sync.Mutex

func SubmitTask(c *gin.Context) {
	var req struct {
		VideoURL  string `json:"video_url"`
		VideoFile string `json:"video_file"`
		Detail    string `json:"detail"`
		Whisper   string `json:"whisper_backend"`
		Replace   []struct {
			Label       string            `json:"label"`
			TimeWindow  [2]float64        `json:"time_window"`
			BBoxPct     map[string]float64 `json:"bbox_pct"`
			ReplaceText string            `json:"replace_text"`
			ReplaceImg  string            `json:"replace_image"`
		} `json:"replacements"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	paramsJSON, _ := json.Marshal(req)
	taskID := fmt.Sprintf("task_%d", time.Now().UnixNano())
	videoPath := req.VideoFile
	if videoPath == "" {
		videoPath = req.VideoURL
	}

	task := &model.Task{
		ID:        taskID,
		VideoPath: videoPath,
		Params:    string(paramsJSON),
		Status:    "pending",
		Progress:  0,
	}
	if err := model.CreateTask(task); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	go service.StartPipeline(taskID, string(paramsJSON))

	c.JSON(http.StatusOK, gin.H{"task_id": taskID, "status": "pending"})
}

func GetTask(c *gin.Context) {
	id := c.Param("id")
	task, err := model.GetTask(id)
	if err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "task not found"})
		return
	}
	c.JSON(http.StatusOK, task)
}

func GetResult(c *gin.Context) {
	id := c.Param("id")
	task, err := model.GetTask(id)
	if err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "task not found"})
		return
	}
	if task.Status != "completed" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "task not completed", "status": task.Status})
		return
	}
	var result map[string]interface{}
	json.Unmarshal([]byte(task.Result), &result)
	c.JSON(http.StatusOK, result)
}

func HandleWS(c *gin.Context) {
	id := c.Param("id")
	conn, err := upgrader.Upgrade(c.Writer, c.Request, nil)
	if err != nil {
		return
	}
	defer conn.Close()

	wsMu.Lock()
	wsClients[id] = append(wsClients[id], conn)
	wsMu.Unlock()

	defer func() {
		wsMu.Lock()
		defer wsMu.Unlock()
		clients := wsClients[id]
		for i, cl := range clients {
			if cl == conn {
				wsClients[id] = append(clients[:i], clients[i+1:]...)
				break
			}
		}
	}()

	for {
		if _, _, err := conn.ReadMessage(); err != nil {
			break
		}
	}
}

func BroadcastProgress(taskID string, data map[string]interface{}) {
	wsMu.Lock()
	clients := wsClients[taskID]
	wsMu.Unlock()
	msg, _ := json.Marshal(data)
	for _, conn := range clients {
		conn.WriteMessage(websocket.TextMessage, msg)
	}
}
