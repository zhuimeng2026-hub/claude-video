package main

import (
	"fmt"
	"log"
	"os"

	"github.com/gin-gonic/gin"

	"video-workflow/config"
	"video-workflow/handler"
	"video-workflow/middleware"
	"video-workflow/model"
	"video-workflow/service"
)

func main() {
	cfg := config.Load()

	if err := model.InitDB(cfg.Database.Path); err != nil {
		log.Fatalf("Failed to init database: %v", err)
	}
	os.MkdirAll(cfg.Pipeline.WorkDir, 0755)

	service.BroadcastFunc = handler.BroadcastProgress

	r := gin.Default()
	r.Use(middleware.CORS())

	r.GET("/api/health", func(c *gin.Context) {
		c.JSON(200, gin.H{"status": "ok"})
	})

	r.POST("/api/workflow/submit", handler.SubmitTask)
	r.GET("/api/workflow/:id", handler.GetTask)
	r.GET("/api/workflow/:id/result", handler.GetResult)
	r.GET("/ws/workflow/:id", handler.HandleWS)

	addr := fmt.Sprintf(":%d", cfg.Server.Port)
	log.Printf("Server starting on %s", addr)
	r.Run(addr)
}
