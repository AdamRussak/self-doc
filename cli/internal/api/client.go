package api

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

type ChunkResult struct {
	ID          int64   `json:"id"`
	Source      string  `json:"source"`
	HeadingPath string  `json:"heading_path"`
	URL         string  `json:"url"`
	Score       float64 `json:"score"`
	Snippet     string  `json:"snippet"`
}

type ChunkDetail struct {
	ID          int64   `json:"id"`
	Source      string  `json:"source"`
	HeadingPath string  `json:"heading_path"`
	URL         string  `json:"url"`
	Content     string  `json:"content"`
	FetchedAt   *string `json:"fetched_at,omitempty"`
}

type SourceNode struct {
	ID         int64   `json:"id"`
	Name       string  `json:"name"`
	BaseURL    string  `json:"base_url"`
	PageCount  int     `json:"page_count"`
	ChunkCount int     `json:"chunk_count"`
	LastSynced *string `json:"last_synced,omitempty"`
}

type Client struct {
	BaseURL    string
	Token      string
	HTTPClient *http.Client
}

func NewClient(baseURL, token string) *Client {
	cleanURL := strings.TrimRight(baseURL, "/")
	if cleanURL == "" {
		cleanURL = "http://localhost:8000"
	}
	return &Client{
		BaseURL: cleanURL,
		Token:   token,
		HTTPClient: &http.Client{
			Timeout: 15 * time.Second,
		},
	}
}

func (c *Client) doRequest(ctx context.Context, method, path string, queryParams url.Values) ([]byte, error) {
	fullURL := c.BaseURL + path
	if len(queryParams) > 0 {
		fullURL += "?" + queryParams.Encode()
	}

	req, err := http.NewRequestWithContext(ctx, method, fullURL, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	if c.Token != "" {
		req.Header.Set("Authorization", "Bearer "+c.Token)
	}

	resp, err := c.HTTPClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("HTTP request failed against %s: %w", c.BaseURL, err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response body: %w", err)
	}

	if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden {
		return nil, fmt.Errorf("authentication failed (HTTP %d): check your token", resp.StatusCode)
	}

	if resp.StatusCode == http.StatusNotFound {
		return nil, fmt.Errorf("resource not found (HTTP 404)")
	}

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		var errResp struct {
			Detail string `json:"detail"`
		}
		if json.Unmarshal(body, &errResp) == nil && errResp.Detail != "" {
			return nil, fmt.Errorf("API error (HTTP %d): %s", resp.StatusCode, errResp.Detail)
		}
		return nil, fmt.Errorf("API error (HTTP %d): %s", resp.StatusCode, string(body))
	}

	return body, nil
}

func (c *Client) Search(ctx context.Context, query string, source string, limit int) ([]ChunkResult, error) {
	params := url.Values{}
	params.Set("q", query)
	if source != "" {
		params.Set("source", source)
	}
	if limit > 0 {
		params.Set("limit", strconv.Itoa(limit))
	}

	body, err := c.doRequest(ctx, http.MethodGet, "/api/v1/search", params)
	if err != nil {
		return nil, err
	}

	var results []ChunkResult
	if err := json.Unmarshal(body, &results); err != nil {
		return nil, fmt.Errorf("failed to decode search results JSON: %w", err)
	}
	return results, nil
}

func (c *Client) GetChunk(ctx context.Context, id int64) (*ChunkDetail, error) {
	path := fmt.Sprintf("/api/v1/chunks/%d", id)
	body, err := c.doRequest(ctx, http.MethodGet, path, nil)
	if err != nil {
		return nil, err
	}

	var detail ChunkDetail
	if err := json.Unmarshal(body, &detail); err != nil {
		return nil, fmt.Errorf("failed to decode chunk detail JSON: %w", err)
	}
	return &detail, nil
}

func (c *Client) GetTree(ctx context.Context) ([]SourceNode, error) {
	body, err := c.doRequest(ctx, http.MethodGet, "/api/v1/tree", nil)
	if err != nil {
		return nil, err
	}

	var tree []SourceNode
	if err := json.Unmarshal(body, &tree); err != nil {
		return nil, fmt.Errorf("failed to decode source tree JSON: %w", err)
	}
	return tree, nil
}

func (c *Client) Ping() error {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	_, err := c.GetTree(ctx)
	return err
}

