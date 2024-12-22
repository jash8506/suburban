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
           and a set of keys which is consistent between each call """

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



        





