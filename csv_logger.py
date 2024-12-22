import logging
from logging.handlers import TimedRotatingFileHandler
import os
import time
import gzip
import shutil

class CSV_Handler(TimedRotatingFileHandler):
    """Combines logger and handler object as well as extending TimedRotatingFileHandler to add header row to CSV logs where necessary."""
    def __init__(self, logger, log_header, *args, **kwargs):
        super(CSV_Handler, self).__init__(*args, **kwargs)
        self.logger = logger
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.logger.addHandler(self)
        self.log_header=log_header
        self._write_header()
    
    def _write_header(self):
        if os.path.getsize(self.baseFilename)==0:
            self.setFormatter(logging.Formatter('%(message)s'))
            self.logger.info(self.log_header)
        self.setFormatter(logging.Formatter(fmt='%(message)s',datefmt='%Y-%m-%d %H:%M:%S'))
    
    def doRollover(self):
        """
        Overwrite doRollover to get it to zip old files and add a header row to new ones.
        
        do a rollover; in this case, a date/time stamp is appended to the filename
        when the rollover happens.  However, you want the file to be named for the
        start of the interval, not the current time.  If there is a backup count,
        then we have to get a list of matching filenames, sort them and remove
        the one with the oldest suffix.
        """
        if self.stream:
            self.stream.close()
            self.stream = None
        # get the time that this sequence started at and make it a TimeTuple
        currentTime = int(time.time())
        dstNow = time.localtime(currentTime)[-1]
        t = self.rolloverAt - self.interval
        if self.utc:
            timeTuple = time.gmtime(t)
        else:
            timeTuple = time.localtime(t)
            dstThen = timeTuple[-1]
            if dstNow != dstThen:
                if dstNow:
                    addend = 3600
                else:
                    addend = -3600
                timeTuple = time.localtime(t + addend)
        dfn = self.baseFilename + " " + time.strftime(self.suffix, timeTuple)
        if os.path.exists(dfn):
            os.remove(dfn)
        # Issue 18940: A file may not have been created if delay is True.
        if os.path.exists(self.baseFilename):
            #os.rename(self.baseFilename, dfn)
            ##Compress instead of rename
            with open(self.baseFilename, 'rb') as f_in, gzip.open(dfn+".gz", 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
            os.remove(self.baseFilename)
        if self.backupCount > 0:
            for s in self.getFilesToDelete():
                os.remove(s)
        if not self.delay:
            self.stream = self._open()
        newRolloverAt = self.computeRollover(currentTime)
        while newRolloverAt <= currentTime:
            newRolloverAt = newRolloverAt + self.interval
        #If DST changes and midnight or weekly rollover, adjust for this.
        if (self.when == 'MIDNIGHT' or self.when.startswith('W')) and not self.utc:
            dstAtRollover = time.localtime(newRolloverAt)[-1]
            if dstNow != dstAtRollover:
                if not dstNow:  # DST kicks in before next rollover, so we need to deduct an hour
                    addend = -3600
                else:           # DST bows out before next rollover, so we need to add an hour
                    addend = 3600
                newRolloverAt += addend
        self.rolloverAt = newRolloverAt
        ##Add header
        self._write_header()