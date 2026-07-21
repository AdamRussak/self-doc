package api

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestClient_Search(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/search" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		if auth := r.Header.Get("Authorization"); auth != "Bearer testtoken" {
			t.Fatalf("unexpected auth header: %s", auth)
		}
		if q := r.URL.Query().Get("q"); q != "fastapi" {
			t.Fatalf("unexpected query q: %s", q)
		}

		res := []ChunkResult{
			{
				ID:          42,
				Source:      "fastapi",
				HeadingPath: "Tutorial > Path",
				URL:         "https://example.com",
				Score:       0.05,
				Snippet:     "FastAPI path params snippet",
			},
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(res)
	}))
	defer server.Close()

	client := NewClient(server.URL, "testtoken")
	results, err := client.Search(context.Background(), "fastapi", "", 3)
	if err != nil {
		t.Fatalf("Search failed: %v", err)
	}

	if len(results) != 1 || results[0].ID != 42 {
		t.Fatalf("unexpected results: %+v", results)
	}
}

func TestClient_GetChunk(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/chunks/42" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		detail := ChunkDetail{
			ID:          42,
			Source:      "fastapi",
			HeadingPath: "Tutorial > Path",
			URL:         "https://example.com",
			Content:     "# Full Markdown Content",
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(detail)
	}))
	defer server.Close()

	client := NewClient(server.URL, "testtoken")
	chunk, err := client.GetChunk(context.Background(), 42)
	if err != nil {
		t.Fatalf("GetChunk failed: %v", err)
	}

	if chunk.ID != 42 || chunk.Content != "# Full Markdown Content" {
		t.Fatalf("unexpected chunk detail: %+v", chunk)
	}
}

func TestClient_GetTree(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/tree" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		nodes := []SourceNode{
			{
				ID:         1,
				Name:       "fastapi",
				BaseURL:    "https://fastapi.tiangolo.com/",
				PageCount:  10,
				ChunkCount: 50,
			},
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(nodes)
	}))
	defer server.Close()

	client := NewClient(server.URL, "testtoken")
	tree, err := client.GetTree(context.Background())
	if err != nil {
		t.Fatalf("GetTree failed: %v", err)
	}

	if len(tree) != 1 || tree[0].Name != "fastapi" {
		t.Fatalf("unexpected tree nodes: %+v", tree)
	}
}

func TestClient_AuthError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, `{"detail":"unauthorized"}`, http.StatusUnauthorized)
	}))
	defer server.Close()

	client := NewClient(server.URL, "badtoken")
	_, err := client.GetTree(context.Background())
	if err == nil {
		t.Fatal("expected auth error, got nil")
	}
}
