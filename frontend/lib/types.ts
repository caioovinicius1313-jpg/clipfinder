export type Project = {
  id: string;
  name: string;
  description: string | null;
  hashtag: string | null;
  created_at: string;
};

export type SourceVideo = {
  id: string;
  project_id: string;
  filename: string;
  path: string;
  size: number;
  duration: number | null;
  status: string;
  created_at: string;
};

export type ReferenceVideo = {
  id: string;
  project_id: string;
  source: "upload" | "youtube";
  title: string;
  filename: string | null;
  path: string | null;
  size: number | null;
  duration: number | null;
  external_id: string | null;
  external_url: string | null;
  thumbnail_url: string | null;
  channel_title: string | null;
  description: string | null;
  status: string;
  created_at: string;
};

export type GeneratedClip = {
  id: string;
  project_id: string;
  source_video_id: string;
  reference_video_id: string;
  original_start_seconds: number | null;
  original_end_seconds: number | null;
  match_score: number | null;
  path: string | null;
  status: "processing" | "completed" | "failed";
  error_message: string | null;
  created_at: string;
};

export type YoutubeSearchResult = {
  video_id: string;
  title: string;
  description: string;
  channel_title: string;
  url: string;
  thumbnail_url: string | null;
  published_at: string | null;
};

export type YoutubeRankingMetric = "views" | "view_delta";

export type HashtagRankingVideo = {
  id: string;
  video_id: string;
  title: string;
  channel_title: string;
  thumbnail_url: string | null;
  published_at: string | null;
  views: number;
  view_delta: number | null;
  snapshot_date: string;
  youtube_url: string;
};

export type HashtagRanking = {
  hashtag: string | null;
  snapshot_date: string | null;
  metric: YoutubeRankingMetric;
  videos: HashtagRankingVideo[];
};

export type ProjectDetail = Project & {
  source_videos: SourceVideo[];
  reference_videos: ReferenceVideo[];
  generated_clips: GeneratedClip[];
};

export type ProjectInput = {
  name: string;
  description: string;
  hashtag: string;
};
