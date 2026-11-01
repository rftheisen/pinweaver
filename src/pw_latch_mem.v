/*
 * 128 x 16 instruction memory built from latches (about half the area of
 * flip-flops plus write muxes).
 *
 * A write is registered on the rising edge (address, data, enable held in
 * flip-flops), then the chosen word's latches are open during the low half
 * of that clock cycle and close on the next rising edge. The latch inputs
 * come straight from flip-flops, so they are stable the whole time a word is
 * open. Writes land half a cycle after a flip-flop memory would have taken
 * them. Reads are combinational, three ports.
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none

module pw_latch_mem (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        we,
    input  wire [6:0]  waddr,
    input  wire [15:0] wdata,
    input  wire [6:0]  raddr0,
    input  wire [6:0]  raddr1,
    input  wire [6:0]  raddr2,
    output wire [15:0] rdata0,
    output wire [15:0] rdata1,
    output wire [15:0] rdata2
);
  reg        w_en;
  reg [6:0]  w_addr;
  reg [15:0] w_data;

  always @(posedge clk) begin
    if (!rst_n) begin
      w_en <= 1'b0; w_addr <= 7'd0; w_data <= 16'd0;
    end else begin
      w_en <= we;
      if (we) begin w_addr <= waddr; w_data <= wdata; end
    end
  end

  wire [15:0] word[0:127];

  genvar i;
  generate
    for (i = 0; i < 128; i = i + 1) begin : g_word
      reg  [15:0] q;
      wire        open_w = w_en && (w_addr == i) && !clk;
      /* verilator lint_off LATCH */
      always @(*) if (open_w) q = w_data;
      /* verilator lint_on LATCH */
      assign word[i] = q;
    end
  endgenerate

  assign rdata0 = word[raddr0];
  assign rdata1 = word[raddr1];
  assign rdata2 = word[raddr2];
endmodule
