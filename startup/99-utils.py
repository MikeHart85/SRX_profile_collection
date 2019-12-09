print(f'Loading {__file__}...')

import os
import numpy as np
from scipy.optimize import curve_fit
from scipy.special import erf
from ophyd import EpicsSignal
from ophyd.utils import make_dir_tree
from bluesky.plans import relative_scan
from bluesky.callbacks import LiveFit,LiveFitPlot
from bluesky.callbacks.fitting import PeakStats
from bluesky.plan_stubs import mv
import lmfit
import time

def breakdown(batch_dir=None, batch_filename=None,xstart=None,ystart=None,\
    xsteps=None,ysteps=None,xstepsize=None,ystepsize=None,zposition=None,\
    acqtime=None,numrois=None,xbasestep=39,ybasestep=39):
    '''
    helper function for hf2dxrf_xybath
    takes a large range with uniform step size and breaks it into chunks

    batch_dir (string): directory for the input batch file
    batch_filename (string): text file name that defines the set points for batch scans
    xstart (float): starting x position
    ystart (float): starting y position
    xsteps (int): steps in X
    ysteps (int): steps in Y
    xstepsize (float): scan step in X
    ystepsize (float): scan step in Y
    zposition (float or list of floats): position(s) in z
    acqtime (float): acquisition time
    numrois (int): number or ROIs
    xbasestep (int): number of X steps in each atomic sub-scan
    ybasestep (int): number of Y steps in each atomic sub-scan
    '''
    xchunks=np.ceil((xsteps+1)/(xbasestep+1))
    ychunks=np.ceil((ysteps+1)/(ybasestep+1))
    xoverflow=np.mod((xsteps+1),(xbasestep+1))-1
    yoverflow=np.mod((ysteps+1),(ybasestep+1))-1
    print('xdimension = '+str(xchunks))
    print('ydimension = '+str(ychunks))
    print('xoverflow = '+str(xoverflow))
    print('yoverflow = '+str(yoverflow))

    if zposition is None:
        zposition=[hf_stage.z.position]
    if zposition.__class__ is not list:
        zposition=[zposition]
    mylist=list()
    for k in zposition:
        for j in range(0,int(ychunks),1):
            for i in range(0,int(xchunks),1):
                xs= xstart+(xbasestep+1)*i*xstepsize
                ys= ystart+(ybasestep+1)*j*ystepsize
                if (ychunks > 1):
                    if ((j==ychunks-1) and (yoverflow >= 0)):
                        ysteps=yoverflow
                    else:
                        ysteps=ybasestep
                if (xchunks>1):
                    if((i==xchunks-1) and (xoverflow >= 0)):
                        xsteps=xoverflow
                    else:
                        xsteps=xbasestep

                mylist.append([k,xs,xsteps,xstepsize,ys,ysteps,ystepsize,\
                acqtime,numrois])
    if batch_dir is None:
        batch_dir = os.getcwd()
        print("No batch_dir was assigned, using the current directory")
    else:
        if not os.path.isdir(batch_dir):
            raise Exception(\
            "Please provide a valid batch_dir for the batch file path.")
    if batch_filename is None:
        raise Exception(\
        "Please provide a batch file name, e.g. batch_file = 'xrf_batch_test.txt'.")
    batchfile = batch_dir+'/'+batch_filename

    with open(batchfile, 'w') as batchf:
        for item in mylist:
            for entry in item:
                batchf.write('%s '%entry)
            batchf.write('\n')
    return mylist

def xybatch_grid(xstart, xstepsize, xnumstep, ystart, ystepsize, ynumstep):
    xylist = []
    for j in np.linspace(ystart, ystart+ystepsize*ynumstep, ynumstep+1):
        for i in np.linspace(xstart, xstart+xstepsize*xnumstep, xnumstep+1):
            xylist.append([i, j])
    return xylist

# Simple Gaussian
def gaussian(x, A, sigma, x0):
    return A*np.exp(-(x - x0)**2/(2 * sigma**2))

# More complete Gaussian with offset and slope
def f_gauss(x, A, sigma, x0, y0, m):
    return y0 + m*x + A*np.exp(-(x - x0)**2/(2 * sigma**2))

# Integral of the Gaussian function with slope and offset
def f_int_gauss(x, A, sigma, x0, y0, m):
    x_star = (x - x0) / sigma
    return A * erf(x_star / np.sqrt(2)) + y0 + m*x

def peakup_dcm(correct_roll=True, plot=False, shutter=True, use_calib=False):
    """

    Scan the HDCM fine pitch and, optionally, roll against the ion chamber in the D Hutch

    correct_roll    <Bool>      If True, align the beam in the vertical (roll)
    plot            <Bool>      If True, plot the intensity as a function of pitch/roll
    shutter         <Bool>      If True, the shutter will be automatically opened/closed
    use_calib       <Bool>      If True, use a previous calibration as an initial guess

    """

    e_value=energy.energy.get()[1]
    pitch_old = dcm.c2_pitch.position
    roll_old = dcm.c1_roll.position

    det = [sclr1]

    ps = PeakStats(dcm.c2_pitch.name, im.name)
    ps1 = PeakStats(dcm.c1_roll.name, im.name)

    if (shutter ==  True):
        RE(mv(shut_b,'Open'))

    # Turn off the ePID loop for the pitch motor
    # 'XF:05IDD-CT{FbPid:02}PID:on'
    c2_pid=EpicsSignal("XF:05IDD-CT{FbPid:02}PID:on")
    c2_pid.put(0)  # Turn off the ePID loop
    c2_V=EpicsSignal("XF:05ID-BI{EM:BPM1}DAC1")
    c2_V.put(3.0)  # Reset the piezo voltage to 3 V

    # pitch_lim = (-19.320, -19.370)
    # pitch_num = 51
    # roll_lim = (-4.9, -5.14)
    # roll_num = 45

    # pitch_lim = (-19.375, -19.425)
    # pitch_num = 51
    # roll_lim = (-4.9, -5.6)
    # roll_num = 51

    pitch_lim = (pitch_old-0.025, pitch_old+0.025)
    roll_lim = (roll_old-0.2, roll_old+0.2)

    pitch_num = 51
    roll_num = 51

    if (use_calib):
        # Factor to convert eV to keV
        K = 1
        if (e_value > 1000):
            K = 1 / 1000
        # Pitch calibration
        pitch_guess = -0.00055357 * K * e_value - 19.39382381
        dcm.c2_pitch.move(pitch_guess, wait=True)
        # Roll calibration
        roll_guess  = -0.01124286 * K * e_value - 4.93568571
        dcm.c1_roll.move(roll_guess, wait=True)
        # Output guess
        print('\nMoving to guess:')
        print('\tC2 Pitch: %f' % (pitch_guess))
        print('\tC1 Roll:  %f\n' % (roll_guess))

    #if e_value < 10.:
    #    sclr1.preset_time.put(0.1)
    #    RE(scan([sclr1], dcm.c2_pitch, -19.335, -19.305, 31), [ps])
    #else:
    #    sclr1.preset_time.put(1.)
    #    RE(scan([sclr1], dcm.c2_pitch, -19.355, -19.310, 46), [ps])
    if e_value < 14.:
        # sclr1.preset_time.put(0.1)
        sclr1.preset_time.put(1.0)
    else:
        sclr1.preset_time.put(1.0)

    if (plot == True):
        sclr1.preset_time.put(1.0)  # Let's collect a longer scan since we're plotting it
        RE(scan(det, dcm.c2_pitch, pitch_lim[0], pitch_lim[1], pitch_num), [ps])
        print('Pitch: Centroid at %f\n\n' % (ps.cen))
        plt.figure()
        plt.plot(ps.x_data, ps.y_data, label='Data')
        plt.plot((ps.cen, ps.cen), (np.amin(ps.y_data), np.amax(ps.y_data)), '--k', label='Centroid')
        plt.xlabel('HDCM C2 PITCH')
        plt.ylabel('Counts')
        plt.legend()
    else:
        RE(scan(det, dcm.c2_pitch, pitch_lim[0], pitch_lim[1], pitch_num), [ps])

    time.sleep(0.5)
    dcm.c2_pitch.move(ps.cen, wait=True)
    time.sleep(0.5)
    if (np.abs(dcm.c2_pitch.position - ps.cen) > 0.001):
        print('The pitch motor did not move on the first try. Trying again...', end='')
        dcm.c2_pitch.move(ps.cen, wait=True)
        if (np.abs(dcm.c2_pitch.position - ps.cen) > 0.001):
            print('FAIL! Check motor location.\n')
        else:
            print('OK\n')
    logscan('peakup_pitch')
    c2pitch_kill.put(1)

    if correct_roll == True:
        if (plot == True):
            sclr1.preset_time.put(1.0)  # If we are debugging, let's collect a longer scan
            RE(scan(det, dcm.c1_roll, roll_lim[0], roll_lim[1], roll_num), [ps1])
            # print('Roll: Maximum flux at %f' % (ps1.max[0]))
            print('Roll: Centroid at %f\n\n' % (ps1.cen))
            plt.figure()
            plt.plot(ps1.x_data, ps1.y_data, label='Data')
            plt.plot((ps1.cen, ps1.cen), (np.amin(ps1.y_data), np.amax(ps1.y_data)), '--k', label='Centroid')
            plt.xlabel('HDCM ROLL')
            plt.ylabel('Counts')
            plt.legend()
        else:
            RE(scan(det, dcm.c1_roll, roll_lim[0], roll_lim[1], roll_num), [ps1])

        time.sleep(0.5)
        dcm.c1_roll.move(ps1.cen,wait=True)
        time.sleep(0.5)
        if (np.abs(dcm.c1_roll.position - ps1.cen) > 0.001):
            print('The roll motor did not move on the first try. Trying again...', end='')
            dcm.c1_roll.move(ps1.cen, wait=True)
            if (np.abs(dcm.c1_roll.position - ps1.cen) > 0.001):
                print('FAIL! Check motor location.\n')
            else:
                print('OK\n')
        logscan('peakup_roll')

    # Output old/new values
    print('Old pitch value:\t%f' % pitch_old)
    print('New pitch value:\t%f' % ps.cen)
    print('Current pitch value:\t%f' % dcm.c2_pitch.position)
    print('Old roll value: \t%f' % roll_old)
    print('New roll value: \t%f' % ps1.cen)
    print('Current roll value: \t%f\n' % dcm.c1_roll.position)

    if (shutter == True):
        RE(mv(shut_b,'Close'))

    #for some reason we now need to kill the pitch motion to keep it from overheating.  6/8/17
    #this need has disappeared mysteriously after the shutdown - gjw 2018/01/19
    # This has now reappeared - amk 2018/06/06
    time.sleep(1)
    c2pitch_kill.put(1)
    c2_pid.put(1)  # Turn on the ePID loop


from scipy.optimize import curve_fit

def peakup_fine(scaler='sclr_i0', plot=True, shutter=True, use_calib=True,
                fix_roll=True, fix_pitch=True):
    """

    Scan the HDCM C2 Piezo Motor to optimize the beam.

    scaler      <String>    Define which scaler channel to maximize on ('sclr_i0', 'sclr_im')
    plot        <Bool>      If True, plot the results
    shutter     <Bool>      If True, the shutter is automatically opened/closed
    use_calib   <Bool>      If True, use lookup table as an initial guess
    fix_roll    <Bool>      If True, peakup C1 roll piezo
    fix_pitch   <Bool>      If True, peakup C2 pitch piezo

    """

    # Get the energy in eV
    E = energy.energy.get()[1]
    if (E < 1000):
        E = E * 1000

    # Define the detector
    det = [sclr1, dcm.c1_roll, dcm.c2_pitch]

    # Set the roll piezo to its default value (3.0)
    # and return the roll to its original value
    rf1_default = 3.0
    total_roll = dcm.c1_roll.position
    # yield from bps.mov(dcm.c1_fine, rf1_default)
    # yield from bps.mov(dcm.c1_roll, total_roll)

    # Set limits
    roll_lim = (2.5, 3.5)
    roll_num = 51

    # Turn off the ePIC loop for the pitch motor
    # c2_pid = EpicsSignal('XF:05IDD-CT{FbPid:02}PID:on')
    # yield from bps.mov(c2_pid, 0)
    yield from bps.mov(dcm.c2_fine.pid_enabled, 0)
    yield from dcm.c2_fine.reset_pid()

    # Set the pitch piezo to its default value (3.0)
    # and return the pitch to its original value
    pf2_default = 3.0
    total_pitch = dcm.c2_pitch.position
    yield from bps.mov(dcm.c2_fine, pf2_default)
    yield from bps.mov(dcm.c2_pitch, total_pitch)
    yield from bps.sleep(1)
    yield from bps.mov(dcm.c2_pitch_kill, 1.0)

    # Set limits
    pitch_lim = (2.5, 3.5)
    pitch_num = 51

    # Use calibration
    if (use_calib):
        # 2018-06-28
        # roll_guess = -0.01124286 * (E/1000) - 4.93568571
        # 2019-02-14
        # roll_guess = -0.00850813 * (E/1000) - 5.01098505
        # 2019-02-14
        # roll_guess = -0.01661758 * (E/1000) - 5.09654066
        # 2019-08-29
        # roll_guess = 0.000
        # 2019-08-29
        # roll_guess = 0.00921 * (E/1000) - 0.380612
        # 2019-11-12
        roll_guess = -0.295
        yield from bps.mov(dcm.c1_roll, roll_guess)
        # 2019-02-14
        # pitch_guess = -0.00106066 * (E/1000) - 19.37338813
        # 2019-04-24
        # pitch_guess = -0.00202462 * (E/1000) - 17.57951692
        # 2019-11-12
        B = energy.energy_to_positions((E/1000), 3, 0)[0]
        pitch_guess = 0.000611 * B + 0.002945
        yield from bps.mov(dcm.c2_pitch, pitch_guess)
        yield from bps.mov(dcm.c2_pitch_kill, 1.0)

    # Set counting time
    sclr1.preset_time.put(1.0)

    # Open the shutter
    if (shutter == True):
        yield from bps.mov(shut_b, 'Open')

    # Setup LiveCallbacks
    plt.figure('Peakup')
    plt.clf()
    livecallbacks = [LivePlot(scaler, dcm.c2_pitch.name,
                              linestyle='', marker='*', color='C0',
                              label='raw',
                              fig=plt.figure('Peakup'))]

    # Run the C2 pitch fine scan
    @subs_decorator(livecallbacks)
    def myplan():
        yield from scan(det, dcm.c2_fine, pitch_lim[0], pitch_lim[1], pitch_num)
    yield from myplan()

    # Close the shutter
    if (shutter == True):
        yield from bps.mov(shut_b, 'Close')

    # Add scan to scanlog
    logscan('peakup_fine_pitch')

    # Collect the data
    h = db[-1]
    # x = h.table()['c2_fine_readback'].values
    x = h.table()['dcm_c2_pitch'].values
    y = h.table()[scaler].values

    # Fit the data
    # gaussian(x, A, sigma, x0):
    y_min = np.amin(y)
    y_max = np.amax(y)
    x_loc = np.argmax(y)
    try:
        popt, _ = curve_fit(f_gauss, x, y, p0=[y_max, 0.001, x[x_loc], 0, 0])
        pitch_new = popt[2]
        print('Maximum flux found at %.4f' % (pitch_new))
    except RuntimeError:
        print('No optimized parameters found.')
        print('Scanning a larger range.')

        # Move total pitch to its original value
        yield from bps.mov(dcm.c2_fine, pf2_default)
        yield from bps.mov(dcm.c2_pitch, total_pitch)
        yield from bps.mov(dcm.c2_pitch_kill, 1.0)

        # Set extended pitch limits
        pitch_lim = (2.0, 4.0)
        pitch_num = 101

        # Set counting time
        sclr1.preset_time.put(1.0)

        # Open the shutter
        if (shutter == True):
            yield from bps.mov(shut_b, 'Open')

        # Run the C2 pitch fine scan
        yield from scan(det, dcm.c2_fine, pitch_lim[0], pitch_lim[1], pitch_num)

        # Close the shutter
        if (shutter == True):
            yield from bps.mov(shut_b, 'Close')

        # Add scan to scanlog
        logscan('peakup_fine_pitch')

        # Collect the data
        h = db[-1]
        # x = h.table()['c2_fine_readback'].values
        x = h.table()['dcm_c2_pitch'].values
        y = h.table()[scaler].values

        # Fit the data
        # gaussian(x, A, sigma, x0):
        y_min = np.amin(y)
        y_max = np.amax(y)
        x_loc = np.argmax(y)

        try:
            popt, _ = curve_fit(f_gauss, x, y, p0=[y_max, 0.001, x[x_loc], 0, 0])
            pitch_new = popt[2]
            print('Maximum flux found at %.4f' % (pitch_new))
        except RuntimeError:
            print('No optimized parameters found.')
            print('Returning to default.')

            pitch_new = total_pitch
            plot = False

    # Move to the maximum
    yield from bps.mov(dcm.c2_fine, pf2_default)
    yield from bps.sleep(1.0)
    ind = 0
    while (np.abs(dcm.c2_pitch.position - pitch_new) > 0.0005):
        yield from bps.mov(dcm.c2_pitch, pitch_new)
        yield from bps.sleep(1.0)
        ind = ind + 1
        if (ind > 5):
            print('Warning: C2 Fine motor might not be in correct location.')
            break

    # Get the new position and set the ePID to that
    yield from bps.mov(dcm.c2_pitch_kill, 1.0)

    # Reset the ePID-I value
    yield from dcm.c2_fine.reset_pid()
    yield from bps.sleep(1.0)
    yield from bps.mov(dcm.c2_fine.pid_enabled, 1)

    # Plot the results
    if (plot == True):
        plt.figure('Peakup')
        # plt.clf()
        # plt.xlabel('C2 Pitch [mrad]')
        # plt.ylabel(scaler + ' [cts]')
        # plt.plot(x, y, 'C0*', label='raw')
        x_plot = np.linspace(x[0], x[-1], num=101)
        y_plot = f_gauss(x_plot, *popt)
        plt.plot(x_plot, y_plot, 'C0--', label='fit')
        plt.plot((pitch_new, pitch_new), (y_min, y_max), '--k', label='max')
        plt.legend()


# Run a knife-edge scan
def knife_edge(motor, start, stop, stepsize, acqtime,
               fly=True, high2low=False, use_trans=True):
    """
    motor       motor   motor used for scan
    start       float   starting position
    stop        float   stopping position
    stepsize    float   distance between data points
    acqtime     float   counting time per step
    fly         bool    if the motor can fly, then fly that motor
    high2low    bool    scan from high transmission to low transmission
                        ex. start will full beam and then block with object (knife/wire)
    """

    # Set detectors
    det = [sclr1]
    if (use_trans == False):
        det.append(xs)

    # Set counting time
    sclr1.preset_time.put(1.0)
    if (use_trans == False):
        xs.settings.acquire_time.put(1.0)

    # Need to convert stepsize to number of points
    num = np.round((stop - start) / stepsize) + 1

    # Run the scan
    if (motor.name == 'hf_stage_y'):
        if fly:
            yield from y_scan_and_fly(start, stop, num,
                                      hf_stage.x.position, hf_stage.x.position+0.001, 1,
                                      acqtime)
        else:
            yield from scan(det, motor, start, stop, num)
    else:
        if fly:
            yield from scan_and_fly(start, stop, num,
                                    hf_stage.y.position, hf_stage.y.position+0.001, 1,
                                    acqtime)
        else:
            # table = LiveTable([motor])
            # @subs_decorator(table)
            # LiveTable([motor])
            yield from scan(det, motor, start, stop, num)

    # Get the information from the previous scan
    haz_data = False
    loop_counter = 0
    MAX_LOOP_COUNTER = 15
    print('Waiting for data...', end='', flush=True)
    while (loop_counter < MAX_LOOP_COUNTER):
        try:
            tbl = db[-1].table('stream0', fill=True)
            haz_data = True
            print('done')
            break
        except:
            loop_counter += 1
            time.sleep(1)

    # Check if we haz data
    if (not haz_data):
        print('Data collection timed out!')
        return
    
    # Get the position information
    if fly:
        pos = 'enc1'
    else:
        pos = motor.name
    # if (motor == hf_stage.y):
    #     pos = 'hf_stage_y'
    # elif (motor == hf_stage.x):
    #     pos = 'hf_stage_x'
    # else:
    #     pos = 'pos'

    # Get the data
    if (use_trans == True):
        y = tbl['it'].values[0] / tbl['im'].values[0]
    else:
        y = np.sum(np.array(tbl['fluor'])[0][:, :, 794:814], axis=(1, 2))
        y = y / np.array(tbl['i0'])[0]
    x = np.array(tbl[pos])[0]
    x = x.astype(np.float64)
    y = y.astype(np.float64)
    dydx = np.gradient(y, x)

    # Fit the raw data
    # def f_int_gauss(x, A, sigma, x0, y0, m)
    p_guess = [0.5*np.amax(y),
               0.001,
               0.5*(x[0] + x[-1]),
               np.amin(y) + 0.5*np.amax(y),
               0.001]
    if high2low:
        p_guess[0] = -0.5 * np.amin(y)
    try:
        popt, _ = curve_fit(f_int_gauss, x, y, p0=p_guess)
    except:
        print('Raw fit failed.')
        popt = p_guess

    # Plot variables
    x_plot = np.linspace(np.amin(x), np.amax(x), num=100)
    y_plot = f_int_gauss(x_plot, *popt)
    dydx_plot = np.gradient(y_plot, x_plot)

    # Display fit of raw data
    plt.figure('Raw')
    plt.clf()
    plt.plot(x, y, '*', label='Raw Data')
    plt.plot(x_plot, f_int_gauss(x_plot, *p_guess), '-', label='Guess fit')
    plt.plot(x_plot, y_plot, '-', label='Final fit')
    plt.legend()

    # Use the fitted raw data to fit a Gaussian
    # def f_gauss(x, A, sigma, x0, y0, m):
    try:
        if (high2low == True):
            p_guess = [np.amin(dydx_plot), popt[1], popt[2], 0, 0]
        else:
            p_guess = [np.amax(dydx_plot), popt[1], popt[2], 0, 0]

        popt2, _ = curve_fit(f_gauss, x_plot, dydx_plot, p0=p_guess)
        # popt2, _ = curve_fit(f_gauss, x, dydx, p0=p_guess)
    except:
        print('Fit failed.')
        popt2 = p_guess


    # Plot the fit
    plt.figure('Derivative')
    plt.clf()
    plt.plot(x, dydx, '*', label='dydx raw')
    plt.plot(x_plot, dydx_plot, '-', label='dydx fit')
    plt.plot(x_plot, f_gauss(x_plot, *p_guess), '-', label='Guess')
    plt.plot(x_plot, f_gauss(x_plot, *popt2), '-', label='Fit')
    plt.legend()

    # Report findings
    C = 2 * np.sqrt(2 * np.log(2))
    print('\nThe beam size is %f um' % (1000 * C * popt2[1]))
    print('The edge is at %.4f mm\n' % (popt2[2]))


def ic_energy_batch(estart,estop,npts):
    ion_chamber_fp=open('/home/xf05id1/current_user_data/ionchamber_readings_'+time.strftime('%Y%m%d%H%M%S')+'.csv','w')
    ion_chamber_fp.write('#energy,I premirror,I sample,I transmittedi\n')
    for i in np.linspace(estart,estop,npts):
        energy.move(i)
        time.sleep(5)
        peakup_dcm()
        time.sleep(5)
        ion_chamber_fp.write('%8.0f,%d,%d,%d\n'%(i,im.get(),i0.get(),it.get()))
    ion_chamber_fp.close()


def retune_undulator():
    energy.detune.put(0.)
    energy.move(energy.energy.get()[0])


def mv_position(pos = []):
    """
    Move to predefined positions of phosphor paper(pos1,default),
    the schitillator(pos2), or the Cu wire(pos3).

    pos     <list> 1 = [22.7, 25.66, 52.596] # phosphor paper
                   2 = [29.18, 19.33, 51.82] # scintillator
                   3 = [27.166, 25.217, 45.859] # Cu wire
                   [x, y, z] # any positions defined
    """
    print('To go to position 1,2,3; Assuming topx, topz at 0; rotation at 45 deg.')

    # Check current positions
    if (pos == []):
        print('You are now in this position: %f, %f, %f. No new positions given.Exiting...' % (hf_stage.x.position, hf_stage.y.position, hf_stage.z.position))
        return

    # Check positions and go there
    if (pos == 1):
        print('Will go to phosphor paper position.')
        pos = [22.7, 25.66, 52.596]
        yield from mv(hf_stage.x, pos[0], hf_stage.y, pos[1], hf_stage.z, pos[2])
    elif (pos == 2):
        print('Will go to scintillator position.')
        pos = [29.18, 19.33, 51.82]
        yield from mv(hf_stage.x, pos[0], hf_stage.y, pos[1], hf_stage.z, pos[2])
    elif (pos == 3):
        print('Will go to Cu horizontal wire position.')
        pos = [27.166, 25.217, 45.859]
        yield from mv(hf_stage.x, pos[0], hf_stage.y, pos[1], hf_stage.z, pos[2])
    elif (len(pos) > 2):
        print('You will move to the defined positions now.')
        yield from mv(hf_stage.x, pos[0], hf_stage.y, pos[1], hf_stage.z, pos[2])
    else:
        print('Not a position, exiting...')
        return


def copyscanparam(src_num,dest_num):
    '''
    Copy all scan paramaters from scan src_num to scan dest_num
    wrapper for cp method in python scanrecord object
    '''
    src = 'scan{}'.format(src_num-1)
    dest = 'scan{}'.format(dest_num-1)
    scanrecord.cp(src,dest)

def printfig():
    plt.savefig('/home/xf05id1/tmp/temp.png', bbox_inches='tight',
    pad_inches=4)
    os.system("lp -d HXN-printer-1 /home/xf05id1/tmp/temp.png")

def estimate_scan_duration(xnum, ynum, dwell, scantype=None, event_delay=None):
    '''
    xnum    int     number of steps or points as entered on the command line for the scan in X
    ynum    int     number of steps or points as entered on the command line for the scan in Y
    dwell   float   exposure time in seconds as entered on the command line
    scantype    string  one of [XRF,XRF_fly,XANES]
    '''
    overhead={'xrf':0.7,'xrf_fly':3.8,'xanes':1.6}
    if event_delay == None:
        try:
            delay =  overhead[scantype.casefold()]
        except KeyError:
            print("Warning:  scantype is not supported")
            delay = 0.
    else:
        delay = event_delay

    if scantype.casefold() == 'xrf_fly':
        if delay is not 0.:
            delay = delay/xnum
        xnum = xnum - 1
        ynum = ynum - 1

    result = ( (xnum + 1) * (ynum + 1) ) * ( dwell + delay )
    div,rem = divmod(result,3600)
    print("Estimated duration is {0:d} hr {1:.1f} min ({2:.1f} sec).".format(int(div),rem/60,result))

    return result

