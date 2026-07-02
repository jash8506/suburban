class DeadbandFilter:    
    """DeadbandFilter(deadbandvalue, maximuminterval)
  deadbandvalue - hysteresis value applied to data
  maximuminterval - maximum update interval in seconds"""
    def __init__(self, deadbandvalue, maximuminterval, debug=False):
        
        # deadbandValue determines the interval over which the filter operates
        self.deadbandValue = deadbandvalue
        
        # maximumInterval determines the maximum time interval between data points
        self.maximumInterval = maximuminterval
        self.debug=debug
        
        # Initialize deadband_data dictionary. This will be used to store deadband tracking
        # data for each of the keys except 'time'
        self.deadband_data = dict()
        self.first_call = True
        self.initialise_filters_flag = True
        self.keys = set()
                
        self.last_datapoint = dict() # last data values used for deadband calculation
        self.save_datapoint = None
        self.last_saved_time = -float('inf')
            
    # store a data point
    def initialise_filter_bounds(self,data_point):
        """Called when a datapoint will be saved."""
        for k in self.keys:
            # new startpoint is last measurement value and time
            start_x = self.last_datapoint['time']
            start_y = self.last_datapoint[k]
    
            # upper line from startpoint to above current measurement 
            # lower line from startpoint to below current measurement
            m_u = (data_point[k]+self.deadbandValue[k] - start_y) / (data_point['time'] - start_x)        
            m_l = (data_point[k]-self.deadbandValue[k] - start_y) / (data_point['time'] - start_x)
            
            self.deadband_data[k] = {
                                        'm_u':m_u,
                                        'b_u':start_y - m_u*start_x,
                                        'm_l':m_l,
                                        'b_l':start_y - m_l*start_x,
                                        'start_x':start_x,
                                        'start_y':start_y
                                    }
    
    def filter(self, data_point):
        """filter() - filter a data point
           data_point is a dictionary which must contain a 'time' key
           and a set of keys which is consistent between each call

           Returns the *previous* data point when it decides one should be
           saved, or None."""

        if self.first_call:
            self.keys = [k for k in data_point.keys() if k != 'time']
            self.last_datapoint = data_point
            self.first_call = False
            return None

        self.save_datapoint = None
        
        # check to see if the previous point has exceeded the maximum interval. If so, we log it and reset bounds.
        dt = self.last_datapoint['time'] - self.last_saved_time
        if  (dt > self.maximumInterval):
            if self.debug:
                print("exceeds maximum time interval. Last Meas Time:{}".format(self.last_saved_time))
            self.initialise_filter_bounds(data_point)
            self.save_datapoint = self.last_datapoint
        
        else:
            # for each key in data_point
            for k in self.keys:
    
                # Test point to see if it falls outside of the trajectory bounds
                y_upper = self.deadband_data[k]['m_u'] * data_point['time'] + self.deadband_data[k]['b_u']
                if(data_point[k] > y_upper):
                    if self.debug:
                        print("{} falls above upper: {}".format(k, y_upper))
                    self.initialise_filter_bounds(data_point)
                    self.save_datapoint = self.last_datapoint
                    continue
                
                else:
                    y_lower = self.deadband_data[k]['m_l'] * data_point['time'] + self.deadband_data[k]['b_l']
                    if(data_point[k] < y_lower):
                        if self.debug:
                            print("{} falls below lower: {}".format(k, y_lower))
                        self.initialise_filter_bounds(data_point)
                        self.save_datapoint = self.last_datapoint 
                        continue
                
                # If the point didn't exceed the bounds of the trajectory update the trajectory, calculate the new trajectory coefficients
                m_u_new = (data_point[k] + self.deadbandValue[k] - self.deadband_data[k]['start_y']) / (data_point['time'] - self.deadband_data[k]['start_x'])
                                
                b_u_new = data_point[k] + self.deadbandValue[k] - m_u_new*data_point['time']
                
                # if new upper limit better than old limit, replace it
                if(m_u_new < self.deadband_data[k]['m_u']):
                    self.deadband_data[k]['m_u'] = m_u_new
                    self.deadband_data[k]['b_u'] = b_u_new
                
                m_l_new = (data_point[k]-self.deadbandValue[k] - self.deadband_data[k]['start_y']) / (data_point['time'] - self.deadband_data[k]['start_x'])
                
                b_l_new = data_point[k]-self.deadbandValue[k] - m_l_new*data_point['time']
                                
                # if new lower limit better than old limit, replace it
                if(m_l_new > self.deadband_data[k]['m_l']):
                    self.deadband_data[k]['m_l'] = m_l_new
                    self.deadband_data[k]['b_l'] = b_l_new

        # update the last_datapoint value
        self.last_datapoint = data_point
        if self.save_datapoint:
            self.last_saved_time = self.save_datapoint['time']
            if self.debug:
                print('Saving ', self.save_datapoint)
        return self.save_datapoint


class DeviceFilter:
    """Deadband-filter one device's field group independently of the other
    devices, so one device going offline can't stall logging for the rest.

    Watches a single key (the device's primary power field) through a
    DeadbandFilter; when that filter decides a sample should be saved, emits
    the full field group from the same sample. A None in the watched key
    means the device has no data at that sample: the last good sample is
    flushed once so the gap edge is recorded, then the filter restarts fresh
    on recovery so pre-gap bounds can't force a spurious save across the gap."""

    def __init__(self, name, watch, fields, deadband, max_interval_s, debug=False):
        self.name = name
        self.watch = watch
        self.fields = fields
        self._deadband = deadband
        self._max_interval_s = max_interval_s
        self._debug = debug
        self._filter = None
        self._last_point = None
        self.in_outage = True

    def process(self, payload):
        """Feed one sample (dict with 'time' plus fields, missing values None).
        Returns a point to log ('time' plus this device's fields), or None."""
        value = payload.get(self.watch)
        if value is None:
            flush = None
            if not self.in_outage:
                # Device just dropped out: emit the last good sample so the
                # gap has a clean edge, and discard filter state.
                flush = self._last_point
                self.in_outage = True
                self._filter = None
                self._last_point = None
            return flush
        if self._filter is None:
            self._filter = DeadbandFilter(
                {self.watch: self._deadband}, self._max_interval_s, debug=self._debug
            )
            self.in_outage = False
        save = self._filter.filter({"time": payload["time"], self.watch: value})
        # The filter returns the *previous* sample it decided to keep;
        # _last_point holds that sample's full field group.
        out = self._last_point if (save and self._last_point) else None
        self._last_point = {
            "time": payload["time"],
            **{k: payload.get(k) for k in self.fields},
        }
        return out



        





