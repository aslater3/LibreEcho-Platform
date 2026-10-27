  return response;
}

void command_set_volume(double volume) {
  // this has a cancellation point if waiting is enabled
  if (config.cmd_set_volume) {
    /*Spawn a child to run the program.*/
    pid_t pid = fork();
    if (pid == 0) { /* child process */
      size_t command_buffer_size = strlen(config.cmd_set_volume) + 32;
      char *command_buffer = (char *)malloc(command_buffer_size);
      if (command_buffer == NULL) {
        inform("Couldn't allocate memory for set_volume argument string");
      } else {
        memset(command_buffer, 0, command_buffer_size);
        snprintf(command_buffer, command_buffer_size, "%s %f", config.cmd_set_volume, volume);
        // debug(1,"command_buffer is \"%s\".",command_buffer);
        int argC;
        char **argV;
        // debug(1,"set_volume command found.");
        if (poptParseArgvString(command_buffer, &argC, (const char ***)&argV) != 0) {
          // note that argV should be free()'d after use, but we expect this fork to exit
          // eventually.
          warn("Can't decipher on-set-volume command arguments \"%s\".", command_buffer);
          free(argV);
          free(command_buffer);
        } else {
          free(command_buffer);
          // debug(1,"Executing on-set-volume command %s with %d arguments.",argV[0],argC);
          execv(argV[0], argV);
          warn("Execution of on-set-volume command \"%s\" failed to start", config.cmd_set_volume);
          // debug(1, "Error executing on-set-volume command %s", config.cmd_set_volume);
          _exit(EXIT_FAILURE); /* only if execv fails */
        }
      }
      _exit(EXIT_SUCCESS);
    } else {
      if (config.cmd_blocking) { /* pid!=0 means parent process and if blocking is true, wait for
                                    process to finish */
        pid_t rc = waitpid(pid, 0, 0); /* wait for child to exit */
        if (rc != pid) {
          warn("Execution of on-set-volume command returned an error.");
          debug(1, "on-set-volume command %s finished with error %d", config.cmd_set_volume, errno);
        }
      }
      // debug(1,"Continue after on-set-volume command");
    }
  }
}

void command_start(void) {
  // this has a cancellation point if waiting is enabled or a response is awaited
  if (config.cmd_start) {
    pid_t pid;
    int pipes[2];

    if (config.cmd_start_returns_output && pipe(pipes) != 0) {
      warn("Unable to allocate pipe for popen of start command.");
      debug(1, "pipe finished with error %d", errno);
      return;
    }
    /*Spawn a child to run the program.*/
    pid = fork();
    if (pid == 0) { /* child process */
      int argC;
      char **argV;

      if (config.cmd_start_returns_output) {
        safe_socket_close(&pipes[0]);
        if (dup2(pipes[1], 1) < 0) {
          warn("Unable to reopen pipe as stdout for popen of start command");
          debug(1, "dup2 finished with error %d", errno);
          safe_socket_close(&pipes[1]);
          return;
        }
      }

      // debug(1,"on-start command found.");
      if (poptParseArgvString(config.cmd_start, &argC, (const char ***)&argV) !=
          0) // note that argV should be free()'d after use, but we expect this fork to exit
             // eventually.
        debug(1, "Can't decipher on-start command arguments");
      else {
        // debug(1,"Executing on-start command %s with %d arguments.",argV[0],argC);
        execv(argV[0], argV);
        warn("Execution of on-start command failed to start");
        debug(1, "Error executing on-start command %s", config.cmd_start);
        _exit(EXIT_FAILURE); /* only if execv fails */
      }
    } else {
      if (config.cmd_blocking || config.cmd_start_returns_output) { /* pid!=0 means parent process
                                    and if blocking is true, wait for
                                    process to finish */
        pid_t rc = waitpid(pid, 0, 0);                              /* wait for child to exit */
        if ((rc != pid) && (errno != ECHILD)) {
          // In this context, ECHILD means that the child process has already completed, I think!
          warn("Execution of on-start command returned an error.");
          debug(1, "on-start command %s finished with error %d", config.cmd_start, errno);
        }
        if (config.cmd_start_returns_output) {
          static char buffer[256];
          int len;
          safe_socket_close(&pipes[1]);
          len = read(pipes[0], buffer, 255);
          safe_socket_close(&pipes[0]);
          buffer[len] = '\0';
          if (buffer[len - 1] == '\n')
            buffer[len - 1] = '\0'; // strip trailing newlines
          debug(1, "received '%s' as the device to use from the on-start command", buffer);
#ifdef CONFIG_ALSA
          set_alsa_out_dev(buffer);
#endif
        }
      }
      // debug(1,"Continue after on-start command");
    }
  }
}
void command_execute(const char *command, const char *extra_argument, const int block) {
  // this has a cancellation point if waiting is enabled
  if (command) {
    char new_command_buffer[2048];
    char *full_command = (char *)command;
    if (extra_argument != NULL) {
      memset(new_command_buffer, 0, sizeof(new_command_buffer));
      snprintf(new_command_buffer, sizeof(new_command_buffer), "%s %s", command, extra_argument);
      full_command = new_command_buffer;
    }

    /*Spawn a child to run the program.*/
    pid_t pid = fork();
    if (pid == 0) { /* child process */
      int argC;
      char **argV;
      if (poptParseArgvString(full_command, &argC, (const char ***)&argV) !=
          0) // note that argV should be free()'d after use, but we expect this fork to exit
             // eventually.
        debug(1, "Can't decipher command arguments in \"%s\".", full_command);
      else {
        // debug(1,"Executing command %s",full_command);
        execv(argV[0], argV);
        warn("Execution of command \"%s\" failed to start", full_command);
        debug(1, "Error executing command \"%s\".", full_command);
        _exit(EXIT_FAILURE); /* only if execv fails */
      }
    } else {
      if (block) { /* pid!=0 means parent process and if blocking is true, wait for
                                    process to finish */
        pid_t rc = waitpid(pid, 0, 0); /* wait for child to exit */
        if ((rc != pid) && (errno != ECHILD)) {
          // In this context, ECHILD means that the child process has already completed, I think!
          warn("Execution of command \"%s\" returned an error.", full_command);
          debug(1, "Command \"%s\" finished with error %d", full_command, errno);
        }
      }
      // debug(1,"Continue after on-unfixable command");
    }
  }
}

void command_stop(void) {
  // this has a cancellation point if waiting is enabled
  if (config.cmd_stop)
    command_execute(config.cmd_stop, "", config.cmd_blocking);
}

// this is for reading an unsigned 32 bit number, such as an RTP timestamp
