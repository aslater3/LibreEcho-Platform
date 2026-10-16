  seq_t last_seqno_read;
  // mutexes and condition variables
  pthread_cond_t flowcontrol;
  pthread_mutex_t ab_mutex, flush_mutex, volume_control_mutex, player_create_delete_mutex;

  int fix_volume;
  double own_airplay_volume;
  int own_airplay_volume_set;

  int ab_buffering, ab_synced;
  uint32_t first_packet_timestamp;
  int flush_requested;
  int flush_output_flushed; // true if the output device has been flushed.
  uint32_t flush_rtp_timestamp;
