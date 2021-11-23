# Copyright (C) 2021 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause
# See: https://spdx.org/licenses/

import numpy as np
from lava.magma.core.sync.protocols.loihi_protocol import LoihiProtocol
from lava.magma.core.model.py.ports import PyInPort, PyOutPort
from lava.magma.core.model.py.type import LavaPyType
from lava.magma.core.resources import CPU
from lava.magma.core.decorator import implements, requires, tag
from lava.magma.core.model.py.model import PyLoihiProcessModel
from lava.proc.lif.process import LIF


@implements(proc=LIF, protocol=LoihiProtocol)
@requires(CPU)
@tag('floating_pt')
class PyLifModelFloat(PyLoihiProcessModel):
    """Implementation of Leaky-Integrate-and-Fire neural process in floating
    point precision. This short and simple ProcessModel can be used for quick
    algorithmic prototyping, without engaging with the nuances of a fixed
    point implementation.
    """
    a_in: PyInPort = LavaPyType(PyInPort.VEC_DENSE, float)
    s_out: PyOutPort = LavaPyType(PyOutPort.VEC_DENSE, bool, precision=1)
    u: np.ndarray = LavaPyType(np.ndarray, float)
    v: np.ndarray = LavaPyType(np.ndarray, float)
    bias: np.ndarray = LavaPyType(np.ndarray, float)
    bias_exp: np.ndarray = LavaPyType(np.ndarray, float)
    du: float = LavaPyType(float, float)
    dv: float = LavaPyType(float, float)
    vth: float = LavaPyType(float, float)

    def run_spk(self):
        a_in_data = self.a_in.recv()
        self.u[:] = self.u * (1 - self.du)
        self.u[:] += a_in_data
        bias = self.bias * (2**self.bias_exp)
        self.v[:] = self.v * (1 - self.dv) + self.u + bias
        s_out = self.v >= self.vth
        self.v[s_out] = 0  # Reset voltage to 0
        self.s_out.send(s_out)


@implements(proc=LIF, protocol=LoihiProtocol)
@requires(CPU)
@tag('bit_accurate_loihi', 'fixed_pt')
class PyLifModelBitAcc(PyLoihiProcessModel):
    """Implementation of Leaky-Integrate-and-Fire neural process bit-accurate
    with Loihi's hardware LIF dynamics, which means, it mimics Loihi
    behaviour bit-by-bit.

    Currently missing features (compared to Loihi 1 hardware):
        - refractory period after spiking
        - axonal delays

    Precisions of state variables
    -----------------------------
    du: unsigned 12-bit integer (0 to 4095)
    dv: unsigned 12-bit integer (0 to 4095)
    bias: signed 13-bit integer (-4096 to 4095). Mantissa part of neuron bias.
    bias_exp: unsigned 3-bit integer (0 to 7). Exponent part of neuron bias.
    vth: unsigned 17-bit integer (0 to 131071)
    """
    a_in: PyInPort = LavaPyType(PyInPort.VEC_DENSE, np.int16, precision=16)
    s_out: PyOutPort = LavaPyType(PyOutPort.VEC_DENSE, bool, precision=1)
    u: np.ndarray = LavaPyType(np.ndarray, np.int32, precision=24)
    v: np.ndarray = LavaPyType(np.ndarray, np.int32, precision=24)
    du: int = LavaPyType(int, np.uint16, precision=12)
    dv: int = LavaPyType(int, np.uint16, precision=12)
    bias: np.ndarray = LavaPyType(np.ndarray, np.int16, precision=13)
    bias_exp: np.ndarray = LavaPyType(np.ndarray, np.int16, precision=3)
    vth: np.ndarray = LavaPyType(np.ndarray, np.int32, precision=17)

    def __init__(self):
        super(PyLifModelBitAcc, self).__init__()
        # ds_offset and dm_offset are 1-bit registers in Loihi 1, which are
        # added to du and dv variables to compute effective decay constants
        # for current and voltage, respectively. They enable setting decay
        # constant values to exact 4096 = 2**12. Without them, the range of
        # 12-bit unsigned du and dv is 0 to 4095.
        # ToDo: Currently, these instance variables cannot be set from
        #  outside, but this will change in the future.
        self.ds_offset = 1
        self.dm_offset = 0
        self.b_vth_computed = False
        self.effective_bias = 0
        self.effective_vth = 0
        # Let's define some bit-widths from Loihi
        # State variables u and v are 24-bits wide
        self.uv_bitwidth = 24
        self.max_uv_val = 2 ** (self.uv_bitwidth - 1)
        # Decays need an MSB alignment with 12-bits
        self.decay_shift = 12
        self.decay_unity = 2 ** self.decay_shift
        # Threshold and incoming activation are MSB-aligned using 6-bits
        self.vth_shift = 6
        self.act_shift = 6

    def run_spk(self):
        # Receive synaptic input
        a_in_data = self.a_in.recv()

        # Compute effective bias and threshold only once, not every time-step
        if not self.b_vth_computed:
            self.effective_bias = np.left_shift(self.bias, self.bias_exp)
            # In Loihi, user specified threshold is just the mantissa, with a
            # constant exponent of 6
            self.effective_vth = np.left_shift(self.vth, self.vth_shift)
            self.b_vth_computed = True

        # Update current
        # --------------
        decay_const_u = self.du + self.ds_offset
        # Below, u is promoted to int64 to avoid overflow of the product
        # between u and decay constant beyond int32. Subsequent right shift by
        # 12 brings us back within 24-bits (and hence, within 32-bits)
        decayed_curr = np.int64(self.u) * (self.decay_unity - decay_const_u)
        decayed_curr = np.sign(decayed_curr) * np.right_shift(np.abs(
            decayed_curr), self.decay_shift)
        decayed_curr = np.int32(decayed_curr)
        # Hardware left-shifts synpatic input for MSB alignment
        a_in_data = np.left_shift(a_in_data, self.act_shift)
        # Add synptic input to decayed current
        decayed_curr += a_in_data
        # Check if value of current is within bounds of 24-bit. Overflows are
        # handled by wrapping around modulo 2 ** 23. E.g., (2 ** 23) + k
        # becomes k and -(2**23 + k) becomes -k
        wrapped_curr = np.mod(decayed_curr,
                              np.sign(decayed_curr) * self.max_uv_val)
        self.u[:] = wrapped_curr
        # Update voltage
        # --------------
        decay_const_v = self.dv + self.dm_offset
        # ToDo: make the exponent 23 configurable (see comment above current
        #  limits)
        neg_voltage_limit = -np.int32(self.max_uv_val) + 1
        pos_voltage_limit = np.int32(self.max_uv_val) - 1
        # Decaying voltage similar to current. See the comment above to
        # understand the need for each of the operations below.
        decayed_volt = np.int64(self.v) * (self.decay_unity - decay_const_v)
        decayed_volt = np.sign(decayed_volt) * np.right_shift(np.abs(
            decayed_volt), self.decay_shift)
        decayed_volt = np.int32(decayed_volt)
        updated_volt = decayed_volt + self.u + self.effective_bias
        self.v[:] = np.clip(updated_volt, neg_voltage_limit, pos_voltage_limit)

        # Spike when exceeds threshold
        # ----------------------------
        s_out = self.v >= self.effective_vth
        # Reset voltage of spiked neurons to 0
        self.v[s_out] = 0
        self.s_out.send(s_out)