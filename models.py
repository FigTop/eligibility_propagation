import tensorflow as tf
import numpy as np
from collections import namedtuple

Cell = tf.contrib.rnn.BasicRNNCell
LightLIFStateTuple = namedtuple('LightLIFStateTuple', ('v', 'z'))


def sum_of_sines_target(seq_len, n_sines=4, periods=[1000, 500, 333, 200], weights=None, phases=None, normalize=True):
    '''
    Generate a target signal as a weighted sum of sinusoids with random weights and phases.
    :param n_sines: number of sinusoids to combine
    :param periods: list of sinusoid periods
    :param weights: weight assigned the sinusoids
    :param phases: phases of the sinusoids
    :return: one dimensional vector of size seq_len contained the weighted sum of sinusoids
    '''
    if periods is None:
        periods = [np.random.uniform(low=100, high=1000) for i in range(n_sines)]
    assert n_sines == len(periods)
    sines = []
    weights = np.random.uniform(low=0.5, high=2, size=n_sines) if weights is None else weights
    phases = np.random.uniform(low=0., high=np.pi * 2, size=n_sines) if phases is None else phases
    for i in range(n_sines):
        sine = np.sin(np.linspace(0 + phases[i], np.pi * 2 * (seq_len // periods[i]) + phases[i], seq_len))
        sines.append(sine * weights[i])

    output = sum(sines)
    if normalize:
        output = output - output[0]
        scale = max(np.abs(np.min(output)), np.abs(np.max(output)))
        output = output / np.maximum(scale, 1e-6)
    return output


def pseudo_derivative(v_scaled, dampening_factor):
    '''
    Define the pseudo derivative used to derive through spikes.
    :param v_scaled: scaled version of the voltage being 0 at threshold and -1 at rest
    :param dampening_factor: parameter that stabilizes learning
    :return:
    '''
    return tf.maximum(1 - tf.abs(v_scaled), 0) * dampening_factor


@tf.custom_gradient
def SpikeFunction(v_scaled, dampening_factor):
    '''
    The tensorflow function which is defined as a Heaviside function (to compute the spikes),
    but with a gradient defined with the pseudo derivative.
    :param v_scaled: scaled version of the voltage being -1 at rest and 0 at the threshold
    :param dampening_factor: parameter to stabilize learning
    :return: the spike tensor
    '''
    z_ = tf.greater(v_scaled, 0.)
    z_ = tf.cast(z_, dtype=tf.float32)

    def grad(dy):
        dE_dz = dy
        dz_dv_scaled = pseudo_derivative(v_scaled, dampening_factor)
        dE_dv_scaled = dE_dz * dz_dv_scaled

        return [dE_dv_scaled,
                tf.zeros_like(dampening_factor)]

    return tf.identity(z_, name="SpikeFunction"), grad


class LightLIF(Cell):
    def __init__(self, n_in, n_rec, tau=20., thr=0.615, dt=1., dtype=tf.float32, dampening_factor=0.3,
                 stop_z_gradients=False):
        '''
        A tensorflow RNN cell model to simulate Learky Integrate and Fire (LIF) neurons.

        WARNING: This model might not be compatible with tensorflow framework extensions because the input and recurrent
        weights are defined with tf.Variable at creation of the cell instead of using variable scopes.

        :param n_in: number of input neurons
        :param n_rec: number of recurrenet neurons
        :param tau: membrane time constant
        :param thr: threshold voltage
        :param dt: time step
        :param dtype: data type
        :param dampening_factor: parameter to stabilize learning
        :param stop_z_gradients: if true, some gradients are stopped to get an equivalence between eprop and bptt
        '''

        self.dampening_factor = dampening_factor
        self.dt = dt
        self.n_in = n_in
        self.n_rec = n_rec
        self.data_type = dtype
        self.stop_z_gradients = stop_z_gradients

        self._num_units = self.n_rec

        self.tau = tf.constant(tau, dtype=dtype)
        self._decay = tf.exp(-dt / self.tau)
        self.thr = thr

        with tf.variable_scope('InputWeights'):
            self.w_in_var = tf.Variable(np.random.randn(n_in, n_rec) / np.sqrt(n_in), dtype=dtype)
            self.w_in_val = tf.identity(self.w_in_var)

        with tf.variable_scope('RecWeights'):
            self.w_rec_var = tf.Variable(np.random.randn(n_rec, n_rec) / np.sqrt(n_rec), dtype=dtype)
            self.recurrent_disconnect_mask = np.diag(np.ones(n_rec, dtype=bool))
            self.w_rec_val = tf.where(self.recurrent_disconnect_mask, tf.zeros_like(self.w_rec_var),
                                      self.w_rec_var)  # Disconnect autotapse

    @property
    def state_size(self):
        return LightLIFStateTuple(v=self.n_rec, z=self.n_rec)

    @property
    def output_size(self):
        return [self.n_rec, self.n_rec]

    def zero_state(self, batch_size, dtype, n_rec=None):
        if n_rec is None: n_rec = self.n_rec

        v0 = tf.zeros(shape=(batch_size, n_rec), dtype=dtype)
        z0 = tf.zeros(shape=(batch_size, n_rec), dtype=dtype)

        return LightLIFStateTuple(v=v0, z=z0)

    def __call__(self, inputs, state, scope=None, dtype=tf.float32):
        thr = self.thr
        z = state.z
        v = state.v
        decay = self._decay

        if self.stop_z_gradients:
            z = tf.stop_gradient(z)

        # update the voltage
        i_t = tf.matmul(inputs, self.w_in_val) + tf.matmul(z, self.w_rec_val)
        I_reset = z * self.thr * self.dt
        new_v = decay * v + (1 - decay) * i_t - I_reset

        # Spike generation
        v_scaled = (new_v - thr) / thr
        new_z = SpikeFunction(v_scaled, self.dampening_factor)
        new_z = new_z * 1 / self.dt
        new_state = LightLIFStateTuple(v=new_v, z=new_z)
        return [new_z, new_v], new_state


def exp_convolve(tensor, decay):
    '''
    Filters a tensor with an exponential filter.
    :param tensor: a tensor of shape (trial, time, neuron)
    :param decay: a decay constant of the form exp(-dt/tau) with tau the time constant
    :return: the filtered tensor of shape (trial, time, neuron)
    '''
    with tf.name_scope('ExpConvolve'):
        assert tensor.dtype in [tf.float16, tf.float32, tf.float64]
        r_shp = range(len(tensor.get_shape()))
        transpose_perm = [1, 0] + list(r_shp)[2:]

        tensor_time_major = tf.transpose(tensor, perm=transpose_perm)
        initializer = tf.zeros_like(tensor_time_major[0])
        filtered_tensor = tf.scan(lambda a, x: a * decay + (1 - decay) * x, tensor_time_major, initializer=initializer)
        filtered_tensor = tf.transpose(filtered_tensor, perm=transpose_perm)
    return filtered_tensor


def shift_by_one_time_step(tensor, initializer=None):
    '''
    Shift the input on the time dimension by one.
    :param tensor: a tensor of shape (trial, time, neuron)
    :param initializer: pre-prend this as the new first element on the time dimension
    :return: a shifted tensor of shape (trial, time, neuron)
    '''
    with tf.name_scope('TimeShift'):
        assert tensor.dtype in [tf.float16, tf.float32, tf.float64]
        r_shp = range(len(tensor.get_shape()))
        transpose_perm = [1, 0] + list(r_shp)[2:]
        tensor_time_major = tf.transpose(tensor, perm=transpose_perm)

        if initializer is None:
            initializer = tf.zeros_like(tensor_time_major[0])

        shifted_tensor = tf.concat([initializer[None, :, :], tensor_time_major[:-1]], axis=0)

        shifted_tensor = tf.transpose(shifted_tensor, perm=transpose_perm)
    return shifted_tensor


def check_gradients(var_list, eprop_grads_np, true_grads_np):
    '''
    Check the correctness of the gradients.
    A ValueError() is raised if the gradients are not almost identical.

    :param var_list: the list of trainable tensorflow variables
    :param eprop_grads_np: a list of numpy arrays containing the gradients obtained eprop
    :param true_grads_np: a list of numpy arrays containing the gradients obtained with bptt
    :return: 
    '''
    for k_v, v in enumerate(var_list):
        eprop_grad = eprop_grads_np[k_v]
        true_grad = true_grads_np[k_v]

        diff = eprop_grad - true_grad
        is_correct = np.abs(diff) < 1e-4

        if np.all(is_correct):
            print('\t' + v.name + ' is correct.')
        else:
            print('\t' + v.name + ' is wrong')
            ratio = np.abs(eprop_grad) / (1e-8 + np.abs(true_grad))
            print('E-prop')
            print(np.array_str(eprop_grad[:5, :5], precision=4))
            print('True gradients')
            print(np.array_str(true_grad[:5, :5], precision=4))
            print('Difference')
            print(np.array_str(diff[:5, :5], precision=4))
            print('Ratio')
            print(np.array_str(ratio[:5, :5], precision=4))

            mismatch_indices = np.where(1 - is_correct)
            mismatch_indices = list(zip(*mismatch_indices))
            print('mismatch indices', mismatch_indices[:5])
            print('diff. vals', [diff[i, j] for i, j in mismatch_indices[:5]])

            raise ValueError()
