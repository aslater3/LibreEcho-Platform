    debug(4,
          "player_volume_without_notification: volume mode is %d, airplay volume is %.2f, "
          "software_attenuation dB: %.2f, hardware_attenuation dB: %.2f, muting "
          "is disabled.",
          volume_mode, airplay_volume, software_attenuation / 100.0, hardware_attenuation / 100.0);
  }
  // here, store the volume for possible use in the future
  config.airplay_volume = airplay_volume;
  conn->own_airplay_volume = airplay_volume;
  pthread_mutex_unlock(&conn->volume_control_mutex);
}

void player_volume(double airplay_volume, rtsp_conn_info *conn) {
  command_set_volume(airplay_volume);
  player_volume_without_notification(airplay_volume, conn);
}

void do_flush(uint32_t timestamp, rtsp_conn_info *conn) {

  debug(3, "do_flush: flush to %u.", timestamp);
  pthread_mutex_lock(&conn->flush_mutex);
  conn->flush_requested = 1;
  conn->flush_rtp_timestamp = timestamp; // flush all packets up to, but not including, this one.
  reset_input_flow_metrics(conn);
#ifdef CONFIG_METADATA
  reset_input_bitrate(conn);
#endif
  pthread_mutex_unlock(&conn->flush_mutex);
}

void player_flush(uint32_t timestamp, rtsp_conn_info *conn) {
  debug(3, "player_flush");
  do_flush(timestamp, conn);
#ifdef CONFIG_CONVOLUTION
  convolver_clear_state();
#endif
#ifdef CONFIG_METADATA
  // only send a flush metadata message if the first packet has been seen -- it's a bogus message
  // otherwise
  if (conn->first_packet_timestamp) {
    char numbuf[32];
    snprintf(numbuf, sizeof(numbuf), "%u", timestamp);
    send_ssnc_metadata('pfls', numbuf, strlen(numbuf), 1); // contains cancellation points
  }
#endif
}

// Export the current session's identity into the environment so that the
// sessioncontrol hooks (run_this_before_play_begins / run_this_after_play_ends)
// can tell a brand-new AirPlay stream from an additional room joining an
// AirPlay 2 group that is already playing. command_start() and command_stop()
// fork and execv, so the child inherits whatever is set here. One
// shairport-sync process serves one session at a time, so there is no
// cross-session race on these variables.
static void export_session_identity(rtsp_conn_info *conn) {
  char numbuf[16];
  if (conn == NULL)
    return;
  setenv("SPS_CLIENT_IP", conn->client_ip_string, 1);
  snprintf(numbuf, sizeof(numbuf), "%u", (unsigned int)conn->connection_number);
  setenv("SPS_CONNECTION_NUMBER", numbuf, 1);
#ifdef CONFIG_AIRPLAY_2
  setenv("SPS_GROUP_ID", conn->airplay_gid ? conn->airplay_gid : "", 1);
  snprintf(numbuf, sizeof(numbuf), "%u", (unsigned int)conn->groupContainsGroupLeader);
  setenv("SPS_GROUP_CONTAINS_LEADER", numbuf, 1);
#else
  setenv("SPS_GROUP_ID", "", 1);
  setenv("SPS_GROUP_CONTAINS_LEADER", "0", 1);
#endif
}

int player_play(rtsp_conn_info *conn) {
  debug(2, "Connection %d: player_play.", conn->connection_number);
  export_session_identity(conn);
  command_start(); // before startup, and before the prepare() method runs
  // give the output device as much advance warning as possible to get ready
  // and make sure it's done before launching the player thread
  if (config.output->prepare)
    config.output->prepare(); // give the backend its first chance to prepare itself, knowing it has
                              // access to the output device (i.e. knowing that it should not be in
                              // use by another program at this time).

  pthread_mutex_lock_and_cleanup_push(&conn->player_create_delete_mutex);
  if (conn->player_thread == NULL) {
    pthread_t *pt = malloc(sizeof(pthread_t));
    if (pt == NULL)
      die("Couldn't allocate space for pthread_t");

    int rc = named_pthread_create_with_priority(pt, 3, player_thread_func, (void *)conn,
                                                "player_%d", conn->connection_number);
    if (rc)
      debug(1, "Connection %d: error creating player_thread: %s", conn->connection_number,
            strerror(errno));
    conn->player_thread = pt; // set _after_ creation of thread
  } else {
    debug(1, "Connection %d: player thread already exists.", conn->connection_number);
  }
  pthread_cleanup_pop(1); // release the player_create_delete_mutex
#ifdef CONFIG_METADATA
  send_ssnc_metadata('pbeg', NULL, 0, 1); // contains cancellation points
#endif
  conn->is_playing = 1;
  return 0;
}

int player_stop(rtsp_conn_info *conn) {
  // note -- this may be called from another connection thread.
  debug(2, "Connection %d: player_stop.", conn->connection_number);
  int response = 0; // okay
  pthread_mutex_lock_and_cleanup_push(&conn->player_create_delete_mutex);
  pthread_t *pt = conn->player_thread;
  if (pt) {
    debug(3, "player_thread cancel...");
    conn->player_thread = NULL; // cleared _before_ cancelling of thread
    pthread_cancel(*pt);
    debug(3, "player_thread join...");
    if (pthread_join(*pt, NULL) == -1) {
      char errorstring[1024];
      strerror_r(errno, (char *)errorstring, sizeof(errorstring));
      debug(1, "Connection %d: error %d joining player thread: \"%s\".", conn->connection_number,
            errno, (char *)errorstring);
    } else {
      debug(2, "Connection %d: player_stop successful.", conn->connection_number);
    }
    free(pt);
    // reset_anchor_info(conn); // say the clock is no longer valid
#ifdef CONFIG_CONVOLUTION
    convolver_clear_state();
#endif
    response = 0; // deleted
  } else {
    debug(2, "Connection %d: no player thread.", conn->connection_number);
    response = -1; // already deleted or never created...
  }
  pthread_cleanup_pop(1); // release the player_create_delete_mutex
  if (response == 0) {    // if the thread was just stopped and deleted...
    conn->is_playing = 0;
/*
// this is done in the player cleanup handler
#ifdef CONFIG_AIRPLAY_2
    ptp_send_control_message_string("E"); // signify play is "E"nding
#endif
*/
#ifdef CONFIG_METADATA
    send_ssnc_metadata('pend', NULL, 0, 1); // contains cancellation points
#endif
    export_session_identity(conn);
    command_stop();
  }
  return response;
}

void buffer_get_frame_cleanup_handler(__attribute__((unused)) void *arg) {
  // rtsp_conn_info *conn = (rtsp_conn_info *)arg;
  // debug_mutex_unlock(&conn->ab_mutex, 0);
}

// get the next frame, when available. return 0 if underrun/stream reset.
static abuf_t *buffer_get_frame(rtsp_conn_info *conn, int resync_requested) {
  // int16_t buf_fill;
  // struct timespec tn;

  /*
  {

                      inframe->timestamp, sync_error_out_of_bounds, centered_sync_error_time * 1000,
                      conn->sync_samples_count, sync_samples_highest_error,
                      sync_samples_lowest_error);

                if (centered_sync_error_time < 0) {
                  request_resync = 1; // ask for a resync
                } else {
                  int16_t occ = conn->ab_write - conn->ab_read;
                  debug(2,
                        "drop late packet, timestamp: %u,  late by: %.3f ms, packets remaining in "
                        "the buffer: %u.",
                        inframe->timestamp, centered_sync_error_time * 1000, occ);
                  unsigned int s;
                  for (s = 0; s < conn->sync_samples_count; s++) {
                    debug(4, "sample: %u, value: %.3f ms", s, sync_samples[s] * 0.000001);
                  }
                  debug(4, "sync_history_length: %u, samples_count: %u, sample_index: %u",
                        SYNC_HISTORY_LENGTH, conn->sync_samples_count, last_sample_index);

            number_of_statistics++;
            frames_since_last_stats_logged += frames_played;
            // stats accumulation. We want an average of sync error, drift, adjustment,
            // number of additions+subtractions
            tsum_of_sync_errors = tsum_of_sync_errors + sync_error;
            tsum_of_corrections = tsum_of_corrections + amount_to_stuff;
            tsum_of_insertions_and_deletions =
                tsum_of_insertions_and_deletions + abs(amount_to_stuff);
          }
        }
        // free buffers and mark the frame as finished
#ifdef CONFIG_FFMPEG
        if (inframe->avframe != NULL) {
          av_frame_free(&inframe->avframe);
          inframe->avframe = NULL;
